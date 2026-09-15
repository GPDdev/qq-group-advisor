#!/usr/bin/env python3
"""Prepare a bounded evidence package from authorized QQChatExporter exports."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import zipfile
from collections import Counter
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from xml.etree import ElementTree as ET


class SourceFormatError(ValueError):
    pass


def _date_bounds(start: str, end: str) -> tuple[float, float]:
    try:
        start_dt = datetime.strptime(start, "%Y-%m-%d")
        end_dt = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)
    except ValueError as exc:
        raise ValueError("日期必须使用 YYYY-MM-DD，且开始/结束日期都不可省略") from exc
    if end_dt <= start_dt:
        raise ValueError("结束日期不得早于开始日期")
    return start_dt.timestamp(), end_dt.timestamp()


def _timestamp_seconds(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value) / 1000 if value > 10_000_000_000 else float(value)
    text = str(value).strip()
    if text.isdigit():
        return _timestamp_seconds(int(text))
    text = text.replace("Z", "+00:00")
    for parser in (datetime.fromisoformat, lambda x: datetime.strptime(x, "%Y/%m/%d %H:%M:%S")):
        try:
            return parser(text).timestamp()
        except ValueError:
            pass
    return None


def _normalize_message(raw: dict[str, Any], chat: dict[str, Any], source: str) -> dict[str, Any]:
    content = raw.get("content")
    if isinstance(content, str):
        content = {"text": content}
    if not isinstance(content, dict):
        content = {}
    sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
    ts = _timestamp_seconds(raw.get("timestamp", raw.get("time")))
    resources = content.get("resources") if isinstance(content.get("resources"), list) else []
    elements = content.get("elements") if isinstance(content.get("elements"), list) else []
    return {
        "id": raw.get("id"),
        "timestamp": ts,
        "time": datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts is not None else None,
        "sender": {
            "uid": sender.get("uid"),
            "uin": sender.get("uin", raw.get("senderUin")),
            "name": sender.get("name", sender.get("nickname", raw.get("senderName", "未知发送者"))),
            "groupCard": sender.get("groupCard"),
        },
        "type": raw.get("type", raw.get("messageType", "unknown")),
        "text": str(content.get("text", raw.get("text", raw.get("message", ""))) or ""),
        "resources": resources,
        "elements": elements,
        "recalled": bool(raw.get("recalled", raw.get("isRecalled", False))),
        "system": bool(raw.get("system", False)),
        "chat": chat,
        "source": source,
    }


def _chat_info(data: dict[str, Any], fallback: str) -> dict[str, Any]:
    info = data.get("chatInfo") if isinstance(data.get("chatInfo"), dict) else {}
    return {
        "id": info.get("id", info.get("uin", fallback)),
        "name": info.get("name", info.get("title", fallback)),
        "type": info.get("type", "unknown"),
    }


def _load_json_file(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, list):
        data = {"messages": data}
    if not isinstance(data, dict):
        raise SourceFormatError(f"JSON 根节点必须是对象或消息数组: {path}")
    if isinstance(data.get("chunked"), dict):
        return _load_chunked_directory(path.parent, data)
    messages = data.get("messages")
    if not isinstance(messages, list):
        raise SourceFormatError(f"JSON 中未找到 messages 数组: {path}")
    chat = _chat_info(data, path.stem)
    return chat, [_normalize_message(m, chat, str(path)) for m in messages if isinstance(m, dict)], []


def _chunk_paths(manifest: dict[str, Any]) -> list[str]:
    chunks = manifest.get("chunked", {}).get("chunks", [])
    paths = []
    for entry in chunks:
        if isinstance(entry, dict):
            value = entry.get("relativePath") or entry.get("fileName")
            if value:
                paths.append(str(value).replace("\\", "/"))
    if not paths:
        raise SourceFormatError("分块 manifest 中没有可读取的 chunks")
    return paths


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise SourceFormatError(f"不安全的分块路径: {value}")
    return path


def _load_chunked_directory(root: Path, manifest: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    chat = _chat_info(manifest, root.name)
    messages = []
    for relative in _chunk_paths(manifest):
        chunk = root.joinpath(*_safe_relative(relative).parts)
        if not chunk.is_file():
            raise SourceFormatError(f"manifest 引用的分块不存在: {chunk}")
        for line_no, line in enumerate(chunk.read_text(encoding="utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SourceFormatError(f"JSONL 解析失败: {chunk}:{line_no}") from exc
            if isinstance(raw, dict):
                messages.append(_normalize_message(raw, chat, str(chunk)))
    return chat, messages, []


def _load_directory(path: Path):
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file():
        raise SourceFormatError(f"目录中没有 manifest.json: {path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    return _load_chunked_directory(path, manifest)


def _load_zip(path: Path):
    with zipfile.ZipFile(path) as zf:
        names = {name.replace("\\", "/"): name for name in zf.namelist() if not name.endswith("/")}
        candidates = [name for name in names if name.endswith("manifest.json")]
        if len(candidates) != 1:
            raise SourceFormatError(f"ZIP 必须恰好包含一个 manifest.json: {path}")
        manifest_name = candidates[0]
        manifest = json.loads(zf.read(names[manifest_name]).decode("utf-8-sig"))
        root = PurePosixPath(manifest_name).parent
        chat = _chat_info(manifest, path.stem)
        messages = []
        for relative in _chunk_paths(manifest):
            target = str(root / _safe_relative(relative))
            if target not in names:
                raise SourceFormatError(f"ZIP 中缺少 manifest 引用的分块: {target}")
            for line_no, line in enumerate(zf.read(names[target]).decode("utf-8-sig").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SourceFormatError(f"ZIP JSONL 解析失败: {target}:{line_no}") from exc
                if isinstance(raw, dict):
                    messages.append(_normalize_message(raw, chat, f"{path}!/{target}"))
        return chat, messages, []


def _load_jsonl(path: Path):
    chat = {"id": path.stem, "name": path.stem, "type": "unknown"}
    messages = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SourceFormatError(f"JSONL 解析失败: {path}:{line_no}") from exc
        if isinstance(raw, dict):
            messages.append(_normalize_message(raw, chat, str(path)))
    return chat, messages, ["独立 JSONL 不含 manifest，群标识取自文件名"]


def _load_text(path: Path):
    text = path.read_text(encoding="utf-8-sig")
    if "QQChatExporter" not in text[:500]:
        raise SourceFormatError(f"TXT 不是可识别的 QQChatExporter 文本导出: {path}")
    name_match = re.search(r"聊天名称\s*[:：]\s*(.+)", text)
    type_match = re.search(r"聊天类型\s*[:：]\s*(.+)", text)
    chat = {
        "id": path.stem,
        "name": name_match.group(1).strip() if name_match else path.stem,
        "type": type_match.group(1).strip() if type_match else "unknown",
    }
    pattern = re.compile(
        r"(?m)^([^\r\n:：]+)[:：]\s*\r?\n时间\s*[:：]\s*([^\r\n]+)\r?\n内容\s*[:：]\s*(.*?)"
        r"(?=\r?\n[^\r\n:：]+[:：]\s*\r?\n时间\s*[:：]|\Z)",
        re.S,
    )
    messages = []
    for index, match in enumerate(pattern.finditer(text), 1):
        sender, time_text, body = match.groups()
        resources = []
        for resource_type, filename in re.findall(r"(?m)^\s*-\s*([^:：]+)[:：]\s*(.+)$", body):
            resources.append({"type": resource_type.strip(), "fileName": filename.strip()})
        body = re.split(r"(?m)^资源\s*[:：]|^提及\s*[:：]|^回复\s*[:：]", body, maxsplit=1)[0].strip()
        raw = {
            "id": f"txt-{index}",
            "time": time_text.strip(),
            "sender": {"name": sender.strip()},
            "type": "text",
            "content": {"text": body, "resources": resources},
        }
        messages.append(_normalize_message(raw, chat, str(path)))
    if not messages:
        raise SourceFormatError(f"TXT 中没有识别到消息块: {path}")
    return chat, messages, []


def _xlsx_cell(cell: ET.Element, shared: list[str], ns: str) -> str:
    kind = cell.get("t")
    value = cell.findtext(f"{{{ns}}}v")
    if kind == "s" and value is not None:
        return shared[int(value)]
    if kind == "inlineStr":
        return "".join(cell.itertext())
    return value or ""


def _excel_serial(value: str) -> str:
    try:
        number = float(value)
    except ValueError:
        return value
    if 20_000 < number < 100_000:
        return (datetime(1899, 12, 30) + timedelta(days=number)).isoformat(sep=" ", timespec="seconds")
    return value


def _load_xlsx(path: Path):
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    with zipfile.ZipFile(path) as zf:
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            shared = ["".join(item.itertext()) for item in root.findall(f"{{{ns}}}si")]
        selected = None
        rows = None
        for sheet_name in sorted(n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")):
            root = ET.fromstring(zf.read(sheet_name))
            candidate = []
            for row in root.findall(f".//{{{ns}}}row"):
                candidate.append([_xlsx_cell(c, shared, ns) for c in row.findall(f"{{{ns}}}c")])
            if candidate and {"时间", "发送者", "消息内容"}.issubset(set(candidate[0])):
                selected, rows = sheet_name, candidate
                break
        if not rows:
            raise SourceFormatError(f"XLSX 中未找到 QCE 聊天记录表头: {path}")
    headers = rows[0]
    chat = {"id": path.stem, "name": path.stem, "type": "unknown"}
    messages = []
    for index, values in enumerate(rows[1:], 1):
        record = dict(zip(headers, values))
        raw = {
            "id": record.get("序号") or f"xlsx-{index}",
            "time": _excel_serial(record.get("时间", "")),
            "sender": {"name": record.get("发送者"), "uin": record.get("发送者QQ号")},
            "messageType": record.get("消息类型"),
            "message": record.get("消息内容"),
            "isRecalled": record.get("是否撤回") in {"是", "true", "TRUE", "1"},
        }
        messages.append(_normalize_message(raw, chat, f"{path}:{selected}"))
    return chat, messages, ["XLSX 主表通常不含媒体文件路径；扩展模式的媒体覆盖率可能不完整"]


class _StructuredHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_json_script = False
        self.scripts: list[str] = []
        self.current: list[str] = []
        self.in_cell = False
        self.row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("type") == "application/json":
            self.in_json_script, self.current = True, []
        if tag in {"td", "th"}:
            self.in_cell, self.current = True, []

    def handle_data(self, data):
        if self.in_json_script or self.in_cell:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.in_json_script:
            self.scripts.append("".join(self.current))
            self.in_json_script = False
        if tag in {"td", "th"} and self.in_cell:
            self.row.append(html.unescape("".join(self.current)).strip())
            self.in_cell = False
        if tag == "tr" and self.row:
            self.rows.append(self.row)
            self.row = []


def _load_html(path: Path):
    parser = _StructuredHTML()
    parser.feed(path.read_text(encoding="utf-8-sig", errors="replace"))
    for block in parser.scripts:
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("messages"), list):
            chat = _chat_info(data, path.stem)
            return chat, [_normalize_message(m, chat, str(path)) for m in data["messages"] if isinstance(m, dict)], []
    if parser.rows and {"时间", "发送者", "消息内容"}.issubset(set(parser.rows[0])):
        headers = parser.rows[0]
        chat = {"id": path.stem, "name": path.stem, "type": "unknown"}
        messages = []
        for index, values in enumerate(parser.rows[1:], 1):
            record = dict(zip(headers, values))
            raw = {
                "id": record.get("序号") or f"html-{index}", "time": record.get("时间"),
                "sender": {"name": record.get("发送者"), "uin": record.get("发送者QQ号")},
                "messageType": record.get("消息类型"), "message": record.get("消息内容"),
            }
            messages.append(_normalize_message(raw, chat, str(path)))
        return chat, messages, ["HTML 表格未提供的字段不会被推断"]
    raise SourceFormatError(f"HTML 中未找到结构化 messages JSON 或聊天记录表格: {path}")


def _load_source(path: Path):
    if path.is_dir():
        return _load_directory(path)
    suffix = path.suffix.lower()
    loaders = {
        ".json": _load_json_file, ".jsonl": _load_jsonl, ".zip": _load_zip,
        ".txt": _load_text, ".xlsx": _load_xlsx, ".html": _load_html, ".htm": _load_html,
    }
    if suffix not in loaders:
        raise SourceFormatError(f"不支持的来源格式: {path}")
    return loaders[suffix](path)


def _identity_key(sender: dict[str, Any]) -> str:
    return str(sender.get("uid") or sender.get("uin") or sender.get("name") or "unknown")


def _media_items(message: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    output, skipped = [], 0
    candidates = list(message.get("resources", [])) + list(message.get("elements", []))
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        media_type = str(raw.get("type", raw.get("elementType", "unknown"))).lower()
        if "video" in media_type or media_type in {"短视频", "视频"}:
            skipped += 1
            continue
        if any(token in media_type for token in ("image", "pic", "face", "sticker", "audio", "voice", "file", "link")):
            output.append({
                "type": media_type,
                "path": raw.get("path") or raw.get("file") or raw.get("fileName") or raw.get("url"),
                "messageId": message.get("id"),
                "time": message.get("time"),
            })
    return output, skipped


def _dedupe_key(message: dict[str, Any]) -> str:
    if message.get("id") not in (None, ""):
        return f"{message['chat'].get('id')}|{message['id']}"
    core = [message["chat"].get("id"), message.get("timestamp"), _identity_key(message["sender"]), message.get("text")]
    return hashlib.sha256(json.dumps(core, ensure_ascii=False).encode()).hexdigest()


def _sample_evenly(messages: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if len(messages) <= limit:
        return messages
    if limit == 1:
        return [messages[len(messages) // 2]]
    return [messages[round(i * (len(messages) - 1) / (limit - 1))] for i in range(limit)]


class _Anonymizer:
    def __init__(self):
        self.people: dict[str, str] = {}
        self.chats: dict[str, str] = {}
        self.replacements: dict[str, str] = {}

    def chat(self, chat: dict[str, Any]) -> dict[str, Any]:
        key = str(chat.get("id") or chat.get("name"))
        alias = self.chats.setdefault(key, f"chat-{len(self.chats) + 1:03d}")
        for value in (chat.get("id"), chat.get("name")):
            if value not in (None, ""):
                self.replacements[str(value)] = alias
        return {"id": alias, "name": alias, "type": chat.get("type", "unknown")}

    def register_sender(self, sender: dict[str, Any]) -> str:
        key = _identity_key(sender)
        alias = self.people.setdefault(key, f"person-{len(self.people) + 1:03d}")
        for value in sender.values():
            if value not in (None, ""):
                self.replacements[str(value)] = alias
        return alias

    def scrub(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self.scrub(item) for item in value]
        if isinstance(value, dict):
            return {key: self.scrub(item) for key, item in value.items()}
        if not isinstance(value, str):
            return value
        for original in sorted(self.replacements, key=len, reverse=True):
            value = value.replace(original, self.replacements[original])
        return re.sub(r"(?<!\d)\d{5,12}(?!\d)", "[qq-id]", value)

    def message(self, message: dict[str, Any], anon_chat: dict[str, Any]) -> dict[str, Any]:
        sender = message["sender"]
        alias = self.register_sender(sender)
        result = dict(message)
        result["chat"] = anon_chat
        result["sender"] = {"id": alias, "name": alias}
        result["text"] = self.scrub(message.get("text", ""))
        result["resources"] = self.scrub(message.get("resources", []))
        result["elements"] = self.scrub(message.get("elements", []))
        result["source"] = Path(message["source"].split("!/")[0].split(":")[0]).name
        return result


def build_evidence(
    sources: Iterable[Path | str], start: str, end: str, privacy: str,
    content_mode: str, max_samples_per_chat: int = 200,
) -> dict[str, Any]:
    if privacy not in {"anonymized", "full"}:
        raise ValueError("privacy 只能是 anonymized 或 full")
    if content_mode not in {"text", "extended"}:
        raise ValueError("content_mode 只能是 text 或 extended")
    if max_samples_per_chat < 1:
        raise ValueError("max_samples_per_chat 必须大于 0")
    start_ts, end_ts = _date_bounds(start, end)
    anonymizer = _Anonymizer()
    chats_output, source_output, warnings = [], [], []
    total_messages = total_video_skipped = 0
    seen: set[str] = set()
    loaded = []

    for value in sources:
        path = Path(value).resolve()
        if not path.exists():
            raise FileNotFoundError(path)
        chat, messages, source_warnings = _load_source(path)
        messages = [m for m in messages if m.get("timestamp") is not None and start_ts <= m["timestamp"] < end_ts]
        loaded.append((path, chat, messages, source_warnings))
        if privacy == "anonymized":
            anonymizer.chat(chat)
            for message in messages:
                anonymizer.register_sender(message["sender"])

    for path, chat, messages, source_warnings in loaded:
        unique = []
        for message in sorted(messages, key=lambda m: m["timestamp"]):
            key = _dedupe_key(message)
            if key not in seen:
                seen.add(key)
                unique.append(message)
        anon_chat = anonymizer.chat(chat) if privacy == "anonymized" else chat
        if privacy == "anonymized":
            unique = [anonymizer.message(m, anon_chat) for m in unique]
        participant_counts = Counter(_identity_key(m["sender"]) for m in unique)
        active_days = Counter(m["time"][:10] for m in unique if m.get("time"))
        hours = Counter(m["time"][11:13] for m in unique if m.get("time"))
        media, skipped = [], 0
        if content_mode == "extended":
            for message in unique:
                found, count = _media_items(message)
                media.extend(found)
                skipped += count
        total_video_skipped += skipped
        total_messages += len(unique)
        sample_fields = ("id", "time", "sender", "type", "text", "recalled", "system")
        samples = [{key: m.get(key) for key in sample_fields} for m in _sample_evenly(unique, max_samples_per_chat)]
        chats_output.append({
            "chat": anon_chat,
            "summary": {
                "messages": len(unique), "participants": len(participant_counts),
                "activeDays": len(active_days), "messagesPerActiveDay": round(len(unique) / len(active_days), 2) if active_days else 0,
                "topParticipantShare": round(max(participant_counts.values()) / len(unique), 4) if unique else 0,
                "hourHistogram": dict(sorted(hours.items())),
            },
            "sampleMessages": samples,
            "mediaCandidates": media,
        })
        source_output.append({
            "source": path.name if privacy == "anonymized" else str(path),
            "chat": anon_chat, "messagesInRange": len(unique), "warnings": source_warnings,
        })
        warnings.extend(source_warnings)

    return {
        "schemaVersion": "qq-group-advisor-evidence-v1",
        "createdAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "requestedRange": {"start": start, "end": end, "endInclusive": True, "timezone": "local"},
        "modes": {"privacy": privacy, "content": content_mode},
        "totals": {"sources": len(source_output), "chats": len(chats_output), "messages": total_messages},
        "coverage": {
            "text": "parsed", "media": "catalogued-not-interpreted" if content_mode == "extended" else "not-requested",
            "video": "skipped", "videoSkipped": total_video_skipped,
        },
        "sources": source_output, "warnings": sorted(set(warnings)), "chats": chats_output,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 QCE 导出整理为 QQ Group Advisor 可审计证据包")
    parser.add_argument("sources", nargs="+", type=Path, help="QCE JSON/JSONL/TXT/XLSX/HTML、分块目录或 ZIP")
    parser.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD（含）")
    parser.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD（含）")
    parser.add_argument("--privacy", required=True, choices=("anonymized", "full"))
    parser.add_argument("--content", required=True, choices=("text", "extended"))
    parser.add_argument("--max-samples-per-chat", type=int, default=200)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        evidence = build_evidence(args.sources, args.start, args.end, args.privacy, args.content, args.max_samples_per_chat)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    print(f"已生成证据包: {args.output}")
    print(f"消息 {evidence['totals']['messages']} 条，群聊/会话 {evidence['totals']['chats']} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
