import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


prepare = load_module("prepare_qq_data", "scripts/prepare_qq_data.py")
napcat = load_module("napcat_actions", "scripts/napcat_actions.py")


def message(mid, ts, name="Alice", uin="12345678", text="hello", elements=None, resources=None):
    return {
        "id": mid,
        "timestamp": ts,
        "sender": {"name": name, "uin": uin},
        "type": "text",
        "content": {"text": text, "elements": elements or [], "resources": resources or []},
        "recalled": False,
        "system": False,
    }


class PrepareQQDataTests(unittest.TestCase):
    def test_json_filters_deduplicates_and_anonymizes(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "group.json"
            source.write_text(json.dumps({
                "chatInfo": {"id": "987654321", "name": "Secret Group", "type": "group"},
                "messages": [
                    message("a", 1767225600000, text="Alice 12345678 and Bob",
                            resources=[{"type": "image", "path": "Alice_12345678.png"}]),
                    message("a", 1767225600000, text="Alice 12345678 and Bob"),
                    message("b", 1767229200000, name="Bob", uin="87654321", text="reply"),
                    message("c", 1769904000000, text="outside"),
                ],
            }), encoding="utf-8")

            evidence = prepare.build_evidence(
                [source], "2026-01-01", "2026-01-31", "anonymized", "extended", 20
            )
            rendered = json.dumps(evidence, ensure_ascii=False)
            self.assertEqual(evidence["totals"]["messages"], 2)
            self.assertNotIn("Alice", rendered)
            self.assertNotIn("Bob", rendered)
            self.assertNotIn("12345678", rendered)
            self.assertNotIn("87654321", rendered)
            self.assertNotIn("987654321", rendered)
            self.assertIn("person-", rendered)
            self.assertIn("chat-", rendered)

    def test_chunked_directory_and_zip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "export"
            chunks = root / "chunks"
            chunks.mkdir(parents=True)
            manifest = {
                "chatInfo": {"id": "g1", "name": "G", "type": "group"},
                "chunked": {"chunks": [{"relativePath": "chunks/c000001.jsonl"}]},
            }
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (chunks / "c000001.jsonl").write_text(
                json.dumps(message("1", 1767225600000)) + "\n", encoding="utf-8"
            )
            direct = prepare.build_evidence([root], "2026-01-01", "2026-01-01", "full", "text", 10)
            self.assertEqual(direct["totals"]["messages"], 1)

            archive = Path(td) / "export.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.write(root / "manifest.json", "manifest.json")
                zf.write(chunks / "c000001.jsonl", "chunks/c000001.jsonl")
            zipped = prepare.build_evidence([archive], "2026-01-01", "2026-01-01", "full", "text", 10)
            self.assertEqual(zipped["totals"]["messages"], 1)

    def test_qce_text_and_extended_media_catalog_skips_video(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "sample.txt"
            source.write_text(
                "[QQChatExporter V5]\n聊天名称: 测试群\n聊天类型: group\n\n"
                "小明:\n时间: 2026-01-02 03:04:05\n内容: 看图\n"
                "资源: 2 个文件\n - image: a.png\n - video: b.mp4\n",
                encoding="utf-8",
            )
            evidence = prepare.build_evidence(
                [source], "2026-01-01", "2026-01-03", "full", "extended", 10
            )
            self.assertEqual(evidence["totals"]["messages"], 1)
            candidates = evidence["chats"][0]["mediaCandidates"]
            self.assertEqual([item["type"] for item in candidates], ["image"])
            self.assertEqual(evidence["coverage"]["videoSkipped"], 1)

    def test_qce_xlsx_table(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "sample.xlsx"
            worksheet = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
<row><c t="inlineStr"><is><t>序号</t></is></c><c t="inlineStr"><is><t>时间</t></is></c><c t="inlineStr"><is><t>发送者</t></is></c><c t="inlineStr"><is><t>发送者QQ号</t></is></c><c t="inlineStr"><is><t>消息类型</t></is></c><c t="inlineStr"><is><t>消息内容</t></is></c></row>
<row><c t="inlineStr"><is><t>1</t></is></c><c t="inlineStr"><is><t>2026-01-02 03:04:05</t></is></c><c t="inlineStr"><is><t>小明</t></is></c><c t="inlineStr"><is><t>123456</t></is></c><c t="inlineStr"><is><t>text</t></is></c><c t="inlineStr"><is><t>测试消息</t></is></c></row>
</sheetData></worksheet>"""
            with zipfile.ZipFile(source, "w") as zf:
                zf.writestr("xl/worksheets/sheet1.xml", worksheet)
            evidence = prepare.build_evidence(
                [source], "2026-01-01", "2026-01-03", "full", "text", 10
            )
            self.assertEqual(evidence["totals"]["messages"], 1)
            self.assertEqual(evidence["chats"][0]["sampleMessages"][0]["text"], "测试消息")

    def test_unknown_format_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "sample.bin"
            source.write_bytes(b"unknown")
            with self.assertRaises(prepare.SourceFormatError):
                prepare.build_evidence([source], "2026-01-01", "2026-01-02", "full", "text", 10)


class NapCatActionTests(unittest.TestCase):
    def test_snapshot_anonymizer_removes_known_identities(self):
        raw = {
            "friends": [{"user_id": 123456, "nickname": "小明"}],
            "groups": [{"group_id": 987654, "group_name": "秘密群"}],
            "selectedGroups": [{
                "groupId": "987654",
                "group_history": {"messages": [{
                    "time": 1767225600, "user_id": 123456,
                    "sender": {"user_id": 123456, "nickname": "小明"},
                    "raw_message": "小明 123456 在秘密群",
                }]},
            }],
        }
        anonymized = napcat.anonymize_snapshot(raw)
        rendered = json.dumps(anonymized, ensure_ascii=False)
        self.assertNotIn("小明", rendered)
        self.assertNotIn("秘密群", rendered)
        self.assertNotIn("123456", rendered)
        self.assertNotIn("987654", rendered)
        self.assertIn("person-001", rendered)
        self.assertIn("chat-001", rendered)

    def test_only_loopback_urls_are_accepted(self):
        napcat.validate_base_url("http://127.0.0.1:3000")
        napcat.validate_base_url("http://localhost:3000")
        with self.assertRaises(ValueError):
            napcat.validate_base_url("https://example.com")

    def test_prepare_requires_one_allowed_action_and_exact_confirmation(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "action.json"
            manifest = napcat.prepare_action(
                "http://127.0.0.1:3000", "send_group_msg",
                {"group_id": "42", "message": "hello"}, path
            )
            self.assertEqual(manifest["status"], "dry-run")
            self.assertNotIn("token", json.dumps(manifest).lower())
            with self.assertRaises(ValueError):
                napcat.execute_action(path, "wrong", token="secret")
            with self.assertRaises(ValueError):
                napcat.prepare_action(
                    "http://127.0.0.1:3000", "send_group_msg",
                    {"group_id": "42", "message": None}, Path(td) / "empty.json"
                )

    def test_execute_posts_once_and_refuses_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "action.json"
            manifest = napcat.prepare_action(
                "http://127.0.0.1:3000", "set_group_name",
                {"group_id": "42", "group_name": "新群名"}, path
            )
            fake = mock.Mock()
            fake.__enter__ = mock.Mock(return_value=fake)
            fake.__exit__ = mock.Mock(return_value=False)
            fake.read.side_effect = [
                json.dumps({"status": "ok", "retcode": 0, "data": {"group_id": 42}}).encode(),
                json.dumps({"status": "ok", "retcode": 0}).encode(),
            ]
            with mock.patch.object(napcat.request, "urlopen", return_value=fake) as urlopen:
                result = napcat.execute_action(path, manifest["confirmationId"], token="secret")
                self.assertEqual(result["response"]["retcode"], 0)
                self.assertEqual(urlopen.call_count, 2)
                self.assertTrue(urlopen.call_args_list[0].args[0].full_url.endswith("/get_group_info"))
                self.assertTrue(urlopen.call_args_list[1].args[0].full_url.endswith("/set_group_name"))
            with self.assertRaises(RuntimeError):
                napcat.execute_action(path, manifest["confirmationId"], token="secret")

    def test_uncertain_write_leaves_attempt_marker_and_cannot_retry(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "action.json"
            manifest = napcat.prepare_action(
                "http://127.0.0.1:3000", "send_group_msg",
                {"group_id": "42", "message": "hello"}, path
            )
            verified = mock.Mock()
            verified.__enter__ = mock.Mock(return_value=verified)
            verified.__exit__ = mock.Mock(return_value=False)
            verified.read.return_value = json.dumps(
                {"status": "ok", "retcode": 0, "data": {"group_id": 42}}
            ).encode()
            with mock.patch.object(
                napcat.request, "urlopen", side_effect=[verified, napcat.error.URLError("timeout")]
            ):
                with self.assertRaises(napcat.error.URLError):
                    napcat.execute_action(path, manifest["confirmationId"], token="secret")
            self.assertTrue(path.with_suffix(path.suffix + ".attempt.json").exists())
            with self.assertRaises(RuntimeError):
                napcat.execute_action(path, manifest["confirmationId"], token="secret")


if __name__ == "__main__":
    unittest.main()
