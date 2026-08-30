"""Deterministic, tool-aware compression for results placed in model context."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping


DEFAULT_TOOL_RESULT_LIMIT = 16_000
HISTORICAL_TOOL_RESULT_LIMIT = 2_400
DIAGNOSTIC_PATTERN = re.compile(
    r"(traceback|assert|error|failed|failure|exception|panic|fatal|\bFAIL\b|\bERROR\b)",
    re.IGNORECASE,
)


def compress_tool_result(
    tool_name: str,
    result_json: str,
    *,
    limit: int = DEFAULT_TOOL_RESULT_LIMIT,
    reference: Mapping[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return valid JSON tailored to the tool while retaining audit metadata."""
    original_chars = len(result_json)
    digest = hashlib.sha256(result_json.encode("utf-8")).hexdigest()
    if original_chars <= limit:
        metadata = _compression_metadata(
            truncated=False,
            original_chars=original_chars,
            returned_chars=original_chars,
            digest=digest,
            strategy="none",
            reference=reference,
        )
        if not reference:
            return result_json, metadata
        original = dict(_json_mapping(result_json))
        original_metadata = original.get("metadata")
        result_metadata = (
            dict(original_metadata) if isinstance(original_metadata, Mapping) else {}
        )
        result_metadata["result_archive"] = {
            key: reference[key]
            for key in ("result_id", "sha256", "tool_name")
            if reference.get(key) is not None
        }
        original["metadata"] = result_metadata
        serialized = json.dumps(original, ensure_ascii=False, separators=(",", ":"))
        metadata["returned_chars"] = len(serialized)
        return serialized, metadata

    original = _json_mapping(result_json)
    success = original.get("success") is True
    value_key = "data" if success else "error"
    value = original.get(value_key)
    strategy = _strategy_for(tool_name, value)
    preview_budget = max(400, limit - 3_200)
    compressed_value = _compress_value(tool_name, value, strategy, preview_budget)
    original_metadata = original.get("metadata")
    compacted: dict[str, Any] = {
        "success": success,
        value_key: compressed_value,
        "metadata": dict(original_metadata) if isinstance(original_metadata, Mapping) else {},
    }
    compression = _compression_metadata(
        truncated=True,
        original_chars=original_chars,
        returned_chars=0,
        digest=digest,
        strategy=strategy,
        reference=reference,
    )
    compression["notice"] = (
        "The complete result is stored locally. Use read_tool_result with result_id "
        "when exact omitted details are needed."
        if reference and reference.get("result_id")
        else "Re-read or rerun the tool when exact omitted details are needed."
    )
    compacted["metadata"]["context_compression"] = compression
    serialized = _serialize_with_stable_length(compacted)
    compression["returned_chars"] = len(serialized)
    compacted["metadata"]["context_compression"] = compression
    serialized = _serialize_with_stable_length(compacted)
    return serialized, dict(compression, returned_chars=len(serialized))


def _strategy_for(tool_name: str, value: Any) -> str:
    if tool_name == "search_code" and isinstance(value, Mapping):
        return "structured_matches"
    if tool_name == "find_files" and isinstance(value, Mapping):
        return "structured_files"
    if tool_name in {"list_files", "read_file"}:
        return "line_window"
    if tool_name == "run_command":
        return "diagnostic_lines"
    if tool_name == "web_search" and isinstance(value, Mapping):
        return "answer_and_sources"
    if tool_name in {"analyze_image", "analyze_pdf"} and isinstance(value, Mapping):
        return "analysis_fields"
    if isinstance(value, Mapping):
        return "structured_json"
    return "head_tail"


def _compress_value(tool_name: str, value: Any, strategy: str, budget: int) -> Any:
    if strategy == "structured_matches" and isinstance(value, Mapping):
        return _bounded_sequence_mapping(value, "matches", budget)
    if strategy == "structured_files" and isinstance(value, Mapping):
        return _bounded_sequence_mapping(value, "files", budget)
    if strategy == "answer_and_sources" and isinstance(value, Mapping):
        answer = _head_tail(str(value.get("answer") or ""), max(200, budget - 4_000))
        sources = value.get("sources")
        return {
            "answer": answer,
            "sources": list(sources) if isinstance(sources, list) else [],
        }
    if strategy == "analysis_fields" and isinstance(value, Mapping):
        result = dict(value)
        analysis = str(result.get("analysis") or "")
        result["analysis"] = _head_tail(analysis, max(200, budget - 1_000))
        return result
    if strategy == "structured_json" and isinstance(value, Mapping):
        return _bounded_json_mapping(value, budget)
    rendered = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, separators=(",", ":")
    )
    if strategy == "line_window":
        return _line_window(rendered, budget)
    if strategy == "diagnostic_lines":
        return _diagnostic_window(rendered, budget)
    return _head_tail(rendered, budget)


def _bounded_json_mapping(value: Mapping[str, Any], budget: int) -> dict[str, Any]:
    """Keep JSON field names and value types instead of slicing serialized JSON."""
    result: dict[str, Any] = {}
    field_budget = max(120, budget // max(1, min(len(value), 12)))
    for index, (key, item) in enumerate(value.items()):
        if index >= 24:
            result["_omitted_fields"] = len(value) - index
            break
        if isinstance(item, str):
            result[str(key)] = _head_tail(item, field_budget)
        elif isinstance(item, Mapping):
            result[str(key)] = _bounded_json_mapping(item, max(120, field_budget))
        elif isinstance(item, list):
            kept: list[Any] = []
            used = 0
            for child in item:
                rendered = json.dumps(child, ensure_ascii=False, separators=(",", ":"))
                if kept and used + len(rendered) > field_budget:
                    break
                kept.append(child if len(rendered) <= field_budget else _head_tail(rendered, field_budget))
                used += min(len(rendered), field_budget)
            result[str(key)] = kept
            if len(kept) < len(item):
                result[f"_{key}_original_count"] = len(item)
        else:
            result[str(key)] = item
    return result


def _bounded_sequence_mapping(
    value: Mapping[str, Any], sequence_key: str, budget: int
) -> dict[str, Any]:
    raw_items = value.get(sequence_key)
    if not isinstance(raw_items, list):
        return {key: item for key, item in value.items() if key != sequence_key}
    kept: list[Any] = []
    used = 0
    for item in raw_items:
        item_size = len(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
        if kept and used + item_size > budget:
            break
        kept.append(item)
        used += item_size
    result = {key: item for key, item in value.items() if key != sequence_key}
    result[sequence_key] = kept
    result["original_count"] = len(raw_items)
    result["returned_count"] = len(kept)
    result["truncated"] = len(kept) < len(raw_items)
    return result


def _line_window(text: str, budget: int) -> str:
    lines = text.splitlines()
    if len(text) <= budget:
        return text
    head_count = max(1, round(len(lines) * 0.72))
    while head_count > 1:
        head = "\n".join(lines[:head_count])
        tail_count = max(1, round(head_count * 0.28 / 0.72))
        tail = "\n".join(lines[-tail_count:])
        candidate = head + "\n… middle lines omitted …\n" + tail
        if len(candidate) <= budget:
            return candidate
        head_count = round(head_count * 0.8)
    return _head_tail(text, budget)


def _diagnostic_window(text: str, budget: int) -> str:
    lines = text.splitlines()
    diagnostics = [line for line in lines if DIAGNOSTIC_PATTERN.search(line)]
    tail = lines[-80:]
    ordered = list(dict.fromkeys([*diagnostics[:120], *tail]))
    rendered = "\n".join(ordered)
    if len(rendered) <= budget:
        return rendered
    return _head_tail(rendered, budget)


def _head_tail(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    marker = "\n… middle content omitted …\n"
    available = max(2, budget - len(marker))
    head = max(1, round(available * 0.72))
    tail = max(1, available - head)
    return text[:head] + marker + text[-tail:]


def _compression_metadata(
    *,
    truncated: bool,
    original_chars: int,
    returned_chars: int,
    digest: str,
    strategy: str,
    reference: Mapping[str, Any] | None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "truncated": truncated,
        "original_chars": original_chars,
        "returned_chars": returned_chars,
        "sha256": digest,
        "strategy": strategy,
    }
    if reference:
        metadata.update(
            {
                key: reference[key]
                for key in ("result_id", "tool_name")
                if reference.get(key) is not None
            }
        )
        if reference.get("sha256") is not None:
            metadata["archive_sha256"] = reference["sha256"]
    return metadata


def _serialize_with_stable_length(payload: dict[str, Any]) -> str:
    serialized = ""
    compression = payload["metadata"]["context_compression"]
    for _ in range(4):
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        compression["returned_chars"] = len(serialized)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _json_mapping(value: str) -> Mapping[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {"success": False, "error": value}
    return parsed if isinstance(parsed, Mapping) else {"success": True, "data": parsed}
