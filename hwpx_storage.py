"""HWP/HWPX 결과의 MongoDB 저장과 검색용 청크 생성.

접속 정보는 환경 변수 또는 .env에서만 읽으며 예외에 원문 URI를 포함하지 않는다.
문서의 active_revision이 가리키는 레코드만 조회하면 미완료 업로드를 제외할 수 있다.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class MongoStorageError(RuntimeError):
    """접속 비밀값을 포함하지 않는 저장 오류."""


def mongo_settings(env_path: Path, database: str | None = None) -> tuple[str, str | None]:
    from dotenv import dotenv_values

    # MongoDB 암호의 ${...}를 dotenv 변수로 확장하지 않는다.
    values = {**dotenv_values(env_path, interpolate=False), **os.environ}
    raw = values.get("MONGODB_URI") or values.get("MONGO_URI") or values.get("client_db")
    if not raw:
        raise ValueError(".env에 MONGODB_URI 또는 client_db 설정이 필요합니다.")
    uri = raw.strip()
    if not uri.startswith(("mongodb://", "mongodb+srv://")):
        # 기존 client_db=MongoClient("...") 설정을 실행 없이 문자열로만 해석한다.
        try:
            expression = ast.parse(uri, mode="eval").body
            if not (isinstance(expression, ast.Call)
                    and isinstance(expression.func, ast.Name)
                    and expression.func.id == "MongoClient"
                    and len(expression.args) == 1 and not expression.keywords):
                raise ValueError
            uri = ast.literal_eval(expression.args[0])
            if not isinstance(uri, str) or not uri.startswith(("mongodb://", "mongodb+srv://")):
                raise ValueError
        except (SyntaxError, ValueError, TypeError):
            raise ValueError("MongoDB 설정 형식이 올바르지 않습니다. URI 문자열을 사용하세요.") from None
    return uri, database or values.get("MONGODB_DATABASE") or None


def split_text(text: str, limit: int = 2000):
    """문단/행 단위 구성 후 긴 내용만 상한에 맞게 분리한다. 원문 범위를 반환한다."""
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            boundary = max(text.rfind("\n", start + limit // 2, end),
                           text.rfind(" ", start + limit // 2, end))
            if boundary > start:
                end = boundary + 1
        yield start, end, text[start:end]
        start = end


def make_chunks(document: dict[str, Any]) -> list[dict[str, Any]]:
    """셀 좌표·병합·출처를 유지한 검색 텍스트. 임베딩/OCR API는 호출하지 않는다."""
    chunks = []
    for block in document["blocks"]:
        kind = block["type"]
        units = []
        if kind == "table":
            for row in block["table"]["cells"]:
                if not row:
                    continue
                text = "\n".join(
                    f"행 {cell['row'] + 1}, 열 {cell['col'] + 1} "
                    f"(행 병합 {cell['row_span']}, 열 병합 {cell['col_span']}): {cell['text']}"
                    for cell in row
                )
                units.append((text, [cell["cell_id"] for cell in row], "table_row_text"))
        elif kind == "paragraph" and block.get("parent_cell_id"):
            # 셀 텍스트는 해당 표의 행 청크에서 이미 검색된다.
            continue
        else:
            units.append((block.get("text", ""), [], "block_text"))
        for unit_index, (text, cell_ids, text_source) in enumerate(units):
            if not text.strip():
                continue
            for start, end, part in split_text(text):
                chunks.append({
                    "chunk_id": f"{block['block_id']}:{unit_index}:{start}",
                    "type": kind, "text": part,
                    "source_refs": [{"block_id": block["block_id"], "cell_ids": cell_ids,
                                     "text_source": text_source, "unit_index": unit_index,
                                     "char_start": start, "char_end": end}],
                    "section": block.get("section"),
                    "physical_page_numbers": block.get("physical_page_numbers", []),
                    "page_numbers": block.get("page_numbers", []),
                    "context_before": block.get("context_before", [])[-1:],
                    "embedding_status": "not_generated",
                })
    return chunks


def prepare_records(document: dict[str, Any]) -> dict[str, Any]:
    """JSON의 호환용 tables/images 복제를 제거하고 BSON 크기를 선검증한다."""
    from bson import BSON

    document_id = document["document_id"]
    # 같은 추출 결과의 재실행은 같은 revision과 _id를 사용한다.
    payload = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    revision = hashlib.sha256(payload.encode()).hexdigest()
    records = {"blocks": [], "assets": [], "chunks": []}
    for block in document["blocks"]:
        record = copy.deepcopy(block)
        # 표 상세는 block.table 한 곳에 저장한다.
        if "table" in record:
            record["table"].pop("context_before", None)
            record["table"].pop("context_after", None)
        records["blocks"].append(record)
    records["assets"] = copy.deepcopy(document.get("assets", []))
    records["chunks"] = make_chunks(document)
    for name, key in (("blocks", "block_id"), ("assets", "asset_id"), ("chunks", "chunk_id")):
        for record in records[name]:
            record.update(_id=f"{document_id}:{revision}:{record[key]}",
                          document_id=document_id, revision=revision)
            if len(BSON.encode(record)) >= 15 * 1024 * 1024:
                raise ValueError(f"{name} 레코드가 너무 큽니다. 표/블록 분할이 필요합니다.")
    metadata = {k: v for k, v in document.items() if k not in {"blocks", "tables", "images", "assets"}}
    # Request에서 문서 전체를 바로 열 수 있도록 추출 구조를 함께 보관한다.
    # 보조 컬렉션은 블록 단위 조회와 AI 검색용으로 유지한다.
    metadata["blocks"] = copy.deepcopy(document["blocks"])
    metadata["assets"] = copy.deepcopy(document.get("assets", []))
    for asset in metadata["assets"]:
        asset["gridfs_id"] = asset["sha256"]
    metadata.update(_id=document_id, active_revision=revision,
                    counts={name: len(items) for name, items in records.items()}, status="ready")
    if len(BSON.encode(metadata)) >= 15 * 1024 * 1024:
        raise ValueError("Request 문서 크기가 15 MiB를 초과했습니다. 문서 분할이 필요합니다.")
    return {"metadata": metadata, **records}


def save_to_mongodb(document: dict[str, Any], source_path: Path, *,
                    env_path: Path, database: str | None = None,
                    store_source: bool = False, collection_name: str = "Request") -> dict[str, Any]:
    """격리된 hwpx_* 컬렉션에 멱등 저장 후 마지막에 활성 revision을 전환한다."""
    from gridfs import GridFS
    from gridfs.errors import FileExists
    from pymongo import MongoClient, ReplaceOne

    records = prepare_records(document)
    uri, db_name = mongo_settings(env_path, database)
    # 연결 전 원본/이미지를 다시 검사해 파일 변경이나 누락을 감지한다.
    files = [(document["source_sha256"], source_path)] if store_source else []
    files.extend((asset["sha256"], Path(asset["local_path"])) for asset in records["assets"])
    for digest, path in files:
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("원본 또는 이미지 파일 해시가 파싱 결과와 다릅니다.")

    stage = "connect"
    try:
        with MongoClient(uri, serverSelectionTimeoutMS=10000, connectTimeoutMS=10000,
                         socketTimeoutMS=60000, appname="hwpx-parser") as client:
            client.admin.command("ping")
            db = client[db_name] if db_name else client.get_default_database(default="Request_for_Proposal")
            requests = db[collection_name]
            fs = GridFS(db, collection="hwpx_files")
            stage = "upload_assets"
            for digest, path in files:
                if not fs.exists(digest):
                    try:
                        with path.open("rb") as stream:
                            fs.put(stream, _id=digest, filename=path.name, metadata={"sha256": digest})
                    except FileExists:
                        # 동일 파일의 동시 업로드가 먼저 완료된 경우만 허용한다.
                        if not fs.exists(digest):
                            raise
            for asset in records["assets"]:
                asset["gridfs_id"] = asset["sha256"]
            for name in ("blocks", "assets", "chunks"):
                stage = f"save_{name}"
                collection = db[f"hwpx_{name}"]
                collection.create_index([("document_id", 1), ("revision", 1)])
                if name == "blocks":
                    collection.create_index([("document_id", 1), ("revision", 1), ("order", 1)])
                values = records[name]
                for offset in range(0, len(values), 500):
                    collection.bulk_write([
                        ReplaceOne({"_id": value["_id"]}, value, upsert=True)
                        for value in values[offset:offset + 500]
                    ], ordered=True)
                expected = len(values)
                actual = collection.count_documents({"document_id": document["document_id"],
                                                     "revision": records["metadata"]["active_revision"]})
                if actual != expected:
                    raise RuntimeError("MongoDB 저장 건수 검증에 실패했습니다.")
            metadata = records["metadata"]
            metadata.update(source_gridfs_id=document["source_sha256"] if store_source else None,
                            stored_at=datetime.now(timezone.utc))
            stage = "publish_revision"
            if not store_source:
                existing = requests.find_one({"_id": metadata["_id"]}, {"source_gridfs_id": 1})
                if existing:
                    metadata["source_gridfs_id"] = existing.get("source_gridfs_id")
            requests.replace_one({"_id": metadata["_id"]}, metadata, upsert=True)
            return {"status": "saved", "database": db.name, "collection": collection_name,
                    "document_id": metadata["_id"],
                    "revision": metadata["active_revision"], "counts": metadata["counts"]}
    except Exception as error:
        # 드라이버 오류 문자열에는 호스트/URI가 포함될 수 있다.
        raise MongoStorageError(
            f"MongoDB 저장 실패: {stage} 단계 ({type(error).__name__}). "
            "접속 설정·네트워크·쓰기 권한을 확인하세요."
        ) from None
