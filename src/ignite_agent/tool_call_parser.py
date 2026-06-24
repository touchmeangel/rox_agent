from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


def parse_text_tool_calls(text: str) -> list[ToolCall]:
    for strategy in (
        _parse_xml_tags,
        _parse_invoke_xml,
        _parse_code_blocks,
        _parse_mistral_prefix,
        _parse_json_array,
        _parse_bare_json_objects,
    ):
        results = strategy(text)
        if results:
            return results
    return []


def _uid(index: int) -> str:
    return f"call_text_{index}_{int(time.time())}"


def _coerce_input(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {}


def _from_payload(payload: dict, index: int) -> ToolCall | None:
    name = payload.get("name")
    if not isinstance(name, str) or not name:
        return None
    raw = payload.get("arguments") or payload.get("parameters") or {}
    return ToolCall(id=_uid(index), name=name, input=_coerce_input(raw))


def _parse_xml_tags(text: str) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for i, m in enumerate(re.finditer(r"<tool_call>\s*(.*?)\s*</tool_call>", text, re.DOTALL)):
        try:
            tc = _from_payload(json.loads(m.group(1)), i)
            if tc:
                calls.append(tc)
        except (json.JSONDecodeError, ValueError):
            pass
    return calls


def _parse_invoke_xml(text: str) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for i, invoke in enumerate(
        re.finditer(r'<invoke\s+name=["\']([^"\']+)["\']>(.*?)</invoke>', text, re.DOTALL)
    ):
        params: dict = {}
        for pm in re.finditer(
            r'<parameter\s+name=["\']([^"\']+)["\']>(.*?)</parameter>',
            invoke.group(2),
            re.DOTALL,
        ):
            raw_val = pm.group(2).strip()
            try:
                params[pm.group(1)] = json.loads(raw_val)
            except json.JSONDecodeError:
                params[pm.group(1)] = raw_val
        calls.append(ToolCall(id=_uid(i), name=invoke.group(1), input=params))
    return calls


def _parse_code_blocks(text: str) -> list[ToolCall]:
    calls: list[ToolCall] = []
    pattern = re.compile(
        r"```(?:json|tool_calls?|function_calls?)?\s*\n(.*?)\n```", re.DOTALL
    )
    for i, m in enumerate(pattern.finditer(text)):
        try:
            payload = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        if isinstance(payload, list):
            for j, item in enumerate(payload):
                if isinstance(item, dict):
                    tc = _from_payload(item, i * 1000 + j)
                    if tc:
                        calls.append(tc)
        elif isinstance(payload, dict):
            tc = _from_payload(payload, i)
            if tc:
                calls.append(tc)
    return calls


def _parse_mistral_prefix(text: str) -> list[ToolCall]:
    m = re.search(r"\[TOOL_CALLS\]\s*(\[.*?\])", text, re.DOTALL)
    if not m:
        return []
    try:
        items = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    calls: list[ToolCall] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        tc = _from_payload(item, i)
        if tc:
            if isinstance(item.get("id"), str):
                tc.id = item["id"]
            calls.append(tc)
    return calls


def _parse_json_array(text: str) -> list[ToolCall]:
    m = re.search(r"\[.*?\]", text, re.DOTALL)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []
    calls: list[ToolCall] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        tc = _from_payload(item, i)
        if tc:
            if isinstance(item.get("id"), str):
                tc.id = item["id"]
            calls.append(tc)
    return calls


def _parse_bare_json_objects(text: str) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for i, obj in enumerate(_extract_json_objects(text)):
        tc = _from_payload(obj, i)
        if tc:
            calls.append(tc)
    return calls


def _extract_json_objects(text: str) -> list[dict]:
    results: list[dict] = []
    depth = 0
    start: int | None = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    parsed = json.loads(text[start : i + 1])
                    if isinstance(parsed, dict):
                        results.append(parsed)
                except json.JSONDecodeError:
                    pass
                start = None
    return results