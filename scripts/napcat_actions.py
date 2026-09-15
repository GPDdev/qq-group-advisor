#!/usr/bin/env python3
"""Read NapCat data and gate a very small set of QQ group write actions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib import error, parse, request


ALLOWED_ACTIONS = {
    "send_group_msg": {"group_id", "message"},
    "set_group_name": {"group_id", "group_name"},
    "set_group_portrait": {"group_id", "file"},
    "send_group_notice": {"group_id", "content"},
}
ENDPOINTS = {**{name: name for name in ALLOWED_ACTIONS}, "send_group_notice": "_send_group_notice"}
READ_ENDPOINTS = {
    "friends": "get_friend_list", "groups": "get_group_list", "group_info": "get_group_info",
    "group_members": "get_group_member_list", "group_history": "get_group_msg_history",
}


def _snapshot_date_bounds(start: str, end: str) -> tuple[float, float]:
    try:
        first = datetime.strptime(start, "%Y-%m-%d")
        after_last = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)
    except ValueError as exc:
        raise ValueError("日期必须使用 YYYY-MM-DD，且开始/结束日期都不可省略") from exc
    if after_last <= first:
        raise ValueError("结束日期不得早于开始日期")
    return first.timestamp(), after_last.timestamp()


def _filter_history(value, start_ts: float, end_ts: float):
    if isinstance(value, list):
        return [_filter_history(item, start_ts, end_ts) for item in value]
    if not isinstance(value, dict):
        return value
    output = {}
    for key, item in value.items():
        if key == "messages" and isinstance(item, list):
            kept = []
            for message in item:
                if not isinstance(message, dict):
                    continue
                stamp = message.get("time", message.get("timestamp"))
                if stamp is None:
                    kept.append(message)
                    continue
                stamp = float(stamp) / 1000 if float(stamp) > 10_000_000_000 else float(stamp)
                if start_ts <= stamp < end_ts:
                    kept.append(message)
            output[key] = [_filter_history(item, start_ts, end_ts) for item in kept]
        else:
            output[key] = _filter_history(item, start_ts, end_ts)
    return output


def anonymize_snapshot(data: dict) -> dict:
    people: dict[str, str] = {}
    chats: dict[str, str] = {}
    replacements: dict[str, str] = {}

    def alias(mapping: dict[str, str], prefix: str, value) -> str:
        key = str(value)
        return mapping.setdefault(key, f"{prefix}-{len(mapping) + 1:03d}")

    def scan(value):
        if isinstance(value, list):
            for item in value:
                scan(item)
            return
        if not isinstance(value, dict):
            return
        group_id = value.get("group_id", value.get("groupId"))
        if group_id not in (None, ""):
            group_alias = alias(chats, "chat", group_id)
            replacements[str(group_id)] = group_alias
            for key in ("group_name", "groupName"):
                if value.get(key) not in (None, ""):
                    replacements[str(value[key])] = group_alias
        person_id = value.get("user_id", value.get("uin", value.get("uid")))
        if person_id not in (None, ""):
            person_alias = alias(people, "person", person_id)
            replacements[str(person_id)] = person_alias
            for key in ("nickname", "card", "name", "remark"):
                if value.get(key) not in (None, ""):
                    replacements[str(value[key])] = person_alias
        for item in value.values():
            scan(item)

    scan(data)

    def scrub_text(value: str) -> str:
        for original in sorted(replacements, key=len, reverse=True):
            value = value.replace(original, replacements[original])
        return re.sub(r"(?<!\d)\d{5,12}(?!\d)", "[qq-id]", value)

    def transform(value, key: str = ""):
        if isinstance(value, list):
            return [transform(item) for item in value]
        if isinstance(value, dict):
            return {field: transform(item, field) for field, item in value.items()}
        text = str(value) if value is not None else ""
        if key in {"group_id", "groupId"} and text:
            return chats.get(text, scrub_text(text))
        if key in {"user_id", "uin", "uid"} and text:
            return people.get(text, scrub_text(text))
        if isinstance(value, str):
            return scrub_text(value)
        return value

    return transform(data)


def validate_base_url(base_url: str) -> str:
    parsed = parse.urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("NapCat 地址必须是本机 HTTP 地址（127.0.0.1、localhost 或 ::1）")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("NapCat 地址不得包含凭据、查询参数或片段")
    return base_url.rstrip("/")


def _call(base_url: str, endpoint: str, payload: dict, token: str, timeout: float) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = request.Request(f"{validate_base_url(base_url)}/{endpoint}", data=body, headers=headers, method="POST")
    # Deliberately one request: uncertain writes must be checked by a human, never retried automatically.
    with request.urlopen(req, timeout=timeout) as response:
        decoded = response.read().decode("utf-8")
    value = json.loads(decoded)
    if not isinstance(value, dict):
        raise RuntimeError("NapCat 返回值不是 JSON 对象")
    return value


def _canonical_action(base_url: str, action: str, payload: dict) -> dict:
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"不允许的写操作: {action}")
    required = ALLOWED_ACTIONS[action]
    missing = required - payload.keys()
    extra = payload.keys() - (required | ({"cache"} if action == "set_group_portrait" else set()))
    if missing or extra:
        raise ValueError(f"参数不匹配；缺少 {sorted(missing)}，多出 {sorted(extra)}")
    empty = sorted(key for key in required if payload.get(key) is None or not str(payload.get(key)).strip())
    if empty:
        raise ValueError(f"必填参数不可为空: {empty}")
    if action == "set_group_portrait":
        payload = {**payload, "cache": int(payload.get("cache", 0))}
    return {"baseUrl": validate_base_url(base_url), "action": action, "endpoint": ENDPOINTS[action], "payload": payload}


def prepare_action(base_url: str, action: str, payload: dict, output: Path) -> dict:
    exact = _canonical_action(base_url, action, payload)
    digest = hashlib.sha256(json.dumps(exact, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
    manifest = {
        "schemaVersion": "qq-group-advisor-napcat-action-v1", "status": "dry-run",
        "createdAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "confirmationId": digest, **exact,
        "instruction": "逐字核对目标群、操作和完整内容；只确认此 confirmationId 对应的一个动作。",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def execute_action(manifest_path: Path, confirmation: str, token: str, timeout: float = 15) -> dict:
    manifest_path = manifest_path.resolve()
    result_path = manifest_path.with_suffix(manifest_path.suffix + ".result.json")
    attempt_path = manifest_path.with_suffix(manifest_path.suffix + ".attempt.json")
    if result_path.exists() or attempt_path.exists():
        raise RuntimeError("该动作已经尝试过，拒绝重复执行；请先在 QQ 中人工核实结果")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    exact = _canonical_action(manifest.get("baseUrl", ""), manifest.get("action", ""), manifest.get("payload", {}))
    expected = hashlib.sha256(json.dumps(exact, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
    if manifest.get("status") != "dry-run" or manifest.get("confirmationId") != expected:
        raise ValueError("dry-run 清单已损坏或被修改，请重新 prepare")
    if confirmation != expected:
        raise ValueError("确认码不匹配，未执行任何操作")
    target = str(exact["payload"]["group_id"])
    group_response = _call(exact["baseUrl"], "get_group_info", {"group_id": target}, token, timeout)
    group_data = group_response.get("data") if isinstance(group_response.get("data"), dict) else {}
    actual = group_data.get("group_id")
    if group_response.get("retcode") not in (None, 0) or actual is None or str(actual) != target:
        raise RuntimeError("NapCat 返回的目标群与 dry-run 不一致，未执行写操作")
    attempt = {
        "schemaVersion": "qq-group-advisor-napcat-attempt-v1", "confirmationId": expected,
        "attemptedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "action": exact["action"], "payload": exact["payload"],
        "note": "该标记在写请求前生成；即使没有结果文件，也禁止自动重试。",
    }
    attempt_path.write_text(json.dumps(attempt, ensure_ascii=False, indent=2), encoding="utf-8")
    response = _call(exact["baseUrl"], exact["endpoint"], exact["payload"], token, timeout)
    result = {
        "schemaVersion": "qq-group-advisor-napcat-result-v1", "confirmationId": expected,
        "executedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "action": exact["action"], "payload": exact["payload"], "response": response,
        "retryPolicy": "never-automatic",
    }
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def snapshot(
    base_url: str, group_ids: list[str], output: Path, start: str, end: str,
    privacy: str, token: str, timeout: float = 30,
) -> dict:
    if privacy not in {"anonymized", "full"}:
        raise ValueError("privacy 只能是 anonymized 或 full")
    start_ts, end_ts = _snapshot_date_bounds(start, end)
    data = {
        "schemaVersion": "qq-group-advisor-napcat-snapshot-v1",
        "createdAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "requestedRange": {"start": start, "end": end, "endInclusive": True, "timezone": "local"},
        "privacy": privacy,
        "friends": _call(base_url, READ_ENDPOINTS["friends"], {}, token, timeout).get("data"),
        "groups": _call(base_url, READ_ENDPOINTS["groups"], {}, token, timeout).get("data"),
        "selectedGroups": [],
    }
    for group_id in group_ids:
        item = {"groupId": group_id}
        for key in ("group_info", "group_members", "group_history"):
            payload = {"group_id": group_id}
            item[key] = _call(base_url, READ_ENDPOINTS[key], payload, token, timeout).get("data")
        data["selectedGroups"].append(item)
    data = _filter_history(data, start_ts, end_ts)
    if privacy == "anonymized":
        data = anonymize_snapshot(data)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def _payload_from_args(args) -> dict:
    payload = {"group_id": args.group_id}
    if args.action == "send_group_msg":
        payload["message"] = args.message
    elif args.action == "set_group_name":
        payload["group_name"] = args.group_name
    elif args.action == "set_group_portrait":
        payload["file"] = args.file
    elif args.action == "send_group_notice":
        payload["content"] = args.content
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NapCat 只读快照与逐项确认写操作")
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument("--token-env", default="NAPCAT_ACCESS_TOKEN", help="令牌环境变量名；令牌本身不会写入文件")
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot")
    snap.add_argument("--group-id", action="append", default=[])
    snap.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD（含）")
    snap.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD（含）")
    snap.add_argument("--privacy", required=True, choices=("anonymized", "full"))
    snap.add_argument("--output", required=True, type=Path)
    prep = sub.add_parser("prepare")
    prep.add_argument("action", choices=sorted(ALLOWED_ACTIONS))
    prep.add_argument("--group-id", required=True)
    prep.add_argument("--message")
    prep.add_argument("--group-name")
    prep.add_argument("--file")
    prep.add_argument("--content")
    prep.add_argument("--output", required=True, type=Path)
    execute = sub.add_parser("execute")
    execute.add_argument("manifest", type=Path)
    execute.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    token = os.environ.get(args.token_env, "")
    try:
        if args.command == "snapshot":
            result = snapshot(args.base_url, args.group_id, args.output, args.start, args.end, args.privacy, token)
            print(f"已保存只读快照: {args.output}（选定群 {len(result['selectedGroups'])} 个）")
        elif args.command == "prepare":
            payload = _payload_from_args(args)
            result = prepare_action(args.base_url, args.action, payload, args.output)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            result = execute_action(args.manifest, args.confirm, token)
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError, error.URLError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
