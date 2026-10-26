import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import monitor
from paper_analyzer import AnalysisWorker, PaperAnalyzer


SUMMARY = "论文\n• 问题：适应新任务。\n• 创新：复用技能。\n• 方法：在线探索。\n• 结果：42 对比 30。\nhttps://example.org/paper\n• 局限性（AI 分析）：仅验证仿真任务，真机泛化尚不明确。"


class ReplyTests(unittest.TestCase):
    def test_reply_targets_original_message_and_uses_stable_deduplication_id(self):
        client = Mock()
        client.im.v1.message.reply.return_value = SimpleNamespace(success=lambda: True, code=0)
        for text in (SUMMARY, "本摘要由 gpt-6-astra 生成\n" + SUMMARY):
            monitor.reply_summary(client, {"message_id": "om_original"}, text)
        requests = [call.args[0] for call in client.im.v1.message.reply.call_args_list]
        self.assertEqual(requests[0].paths["message_id"], "om_original")
        self.assertTrue(requests[0].body.reply_in_thread)
        self.assertEqual(requests[0].body.msg_type, "post")
        post = json.loads(requests[0].body.content)["zh_cn"]
        self.assertEqual(post["title"], "📄 论文")
        self.assertEqual(post["content"][0], [
            {"tag": "text", "text": "🤖 本摘要由 gpt-6-astra 生成", "style": ["italic"]}])
        headings = [row[0] for row in post["content"] if row[0].get("style") == ["bold"]]
        self.assertEqual([node["text"] for node in headings],
                         ["🎯 问题：", "💡 创新：", "🛠️ 方法：", "📊 结果：", "⚠️ 局限性（AI 分析）："])
        self.assertEqual(post["content"][-1], [
            {"tag": "text", "text": "仅验证仿真任务，真机泛化尚不明确。"}])
        self.assertIn([{"tag": "text", "text": "42 对比 30。"}], post["content"])
        self.assertIn([{"tag": "a", "text": "🔗 阅读原文", "href": "https://example.org/paper"}], post["content"])
        self.assertEqual(requests[0].body.content, requests[1].body.content)
        self.assertEqual(requests[0].body.uuid, requests[1].body.uuid)
        self.assertLessEqual(len(requests[0].body.uuid), 50)
        monitor.reply_summary(client, {"message_id": "om_other"}, SUMMARY)
        self.assertNotEqual(requests[0].body.uuid, client.im.v1.message.reply.call_args.args[0].body.uuid)

    def test_rich_reply_preserves_continuations_notes_and_literal_markup(self):
        client = Mock()
        client.im.v1.message.reply.return_value = SimpleNamespace(success=lambda: True, code=0)
        summary = SUMMARY.replace("• 问题", "问题").replace(
            "• 方法：在线探索。", "• 方法：在线探索。\n  保留 <b>原始文本</b> 与 100% 数据。")
        summary += "\n备注：仅仿真，未验证真机。"
        monitor.reply_summary(client, {"message_id": "om_original"}, summary)
        post = json.loads(client.im.v1.message.reply.call_args.args[0].body.content)["zh_cn"]
        nodes = [node for row in post["content"] for node in row]
        self.assertIn({"tag": "text", "text": "🎯 问题：", "style": ["bold"]}, nodes)
        self.assertIn({"tag": "text", "text": "保留 <b>原始文本</b> 与 100% 数据。"}, nodes)
        self.assertIn({"tag": "text", "text": "仅仿真，未验证真机。"}, nodes)

    def test_rich_reply_preserves_proxy_model_disclosure_without_duplicate_header(self):
        client = Mock()
        client.im.v1.message.reply.return_value = SimpleNamespace(success=lambda: True, code=0)
        monitor.reply_summary(client, {"message_id": "om_original"},
                              "本摘要由 local-proxy-model 生成\n" + SUMMARY)
        post = json.loads(client.im.v1.message.reply.call_args.args[0].body.content)["zh_cn"]
        self.assertEqual(post["title"], "📄 论文")
        self.assertEqual(post["content"][0][0]["text"], "🤖 本摘要由 local-proxy-model 生成")

    def test_api_failure_reports_code_without_exposing_response_body(self):
        client = Mock()
        client.im.v1.message.reply.return_value = SimpleNamespace(
            success=lambda: False, code=99991672, msg="private response")
        with self.assertRaisesRegex(RuntimeError, "99991672") as caught:
            monitor.reply_summary(client, {"message_id": "om_original"}, SUMMARY)
        self.assertNotIn("private response", str(caught.exception))

    def test_missing_original_message_is_reported_as_a_topic_error(self):
        client = Mock()
        client.im.v1.message.reply.return_value = SimpleNamespace(success=lambda: False, code=230019)
        with self.assertRaisesRegex(RuntimeError, "230019.*topic no longer exists"):
            monitor.reply_summary(client, {"message_id": "om_recalled"}, SUMMARY)

    def test_invalid_or_failed_summaries_are_never_sent(self):
        client = Mock()
        for text in ("", "Analysis unavailable: HTTP 403", "Analysis unavailable: " + SUMMARY,
                     "x" * 1001, "ordinary chatter", SUMMARY.split("\n• 局限性")[0]):
            with self.subTest(text=text), self.assertRaises(ValueError):
                monitor.reply_summary(client, {"message_id": "om_original"}, text)
        client.im.v1.message.reply.assert_not_called()

    def test_connection_error_is_reported_without_private_exception_details(self):
        from requests import ConnectionError

        client = Mock()
        client.im.v1.message.reply.side_effect = ConnectionError("private request details")
        with self.assertRaisesRegex(RuntimeError, "connection failed") as caught:
            monitor.reply_summary(client, {"message_id": "om_original"}, SUMMARY)
        self.assertNotIn("private request details", str(caught.exception))

    def test_disclosure_counts_towards_the_1000_character_limit(self):
        client = Mock()
        summary = SUMMARY + "。" * (1000 - len(SUMMARY))
        with self.assertRaises(ValueError):
            monitor.reply_summary(client, {"message_id": "om_original"}, summary)
        client.im.v1.message.reply.assert_not_called()

    def test_nonresearch_message_creates_no_summary_and_does_not_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            analyzer = PaperAnalyzer(output_dir=Path(directory) / "analyses")
            with patch("paper_analyzer.discover", return_value=([], "not_research: greeting")), \
                    patch("paper_analyzer.archive_papers") as archive, \
                    patch.object(analyzer, "_complete") as summarize:
                result = analyzer.analyze({"message_id": "om_chatter", "content": "大家好"})
            self.assertIsNone(result)
            self.assertFalse(analyzer.output_dir.exists())
            archive.assert_not_called()
            summarize.assert_not_called()

    def test_worker_replies_only_to_successful_summaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failed = root / "failed.md"
            failed.write_text("Analysis unavailable: HTTP 403")
            report = root / "summary.md"
            report.write_text(SUMMARY)
            analyzer = Mock()
            analyzer.analyze.side_effect = [None, failed, report]
            delivered = []
            output = io.StringIO()
            worker = AnalysisWorker(analyzer, output, reply=lambda message, text: delivered.append((message["message_id"], text)))
            try:
                for identifier in ("chatter", "unreadable", "paper"):
                    worker.submit({"message_id": identifier, "content": identifier})
            finally:
                worker.close()
            self.assertEqual(delivered, [("paper", SUMMARY)])
            self.assertIn("Skipped", output.getvalue())
            self.assertIn("Analysis unavailable", output.getvalue())
            self.assertEqual(report.read_text(), SUMMARY)

    def test_reply_failure_keeps_report_and_worker_processes_next_message(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "summary.md"
            report.write_text(SUMMARY)
            analyzer = Mock()
            analyzer.analyze.return_value = report
            delivered = []

            def reply(message, text):
                if message["message_id"] == "first":
                    raise RuntimeError("permission denied")
                delivered.append((message["message_id"], text))

            output = io.StringIO()
            worker = AnalysisWorker(analyzer, output, reply=reply)
            try:
                for identifier in ("first", "second"):
                    worker.submit({"message_id": identifier, "content": "paper"})
            finally:
                worker.close()
            self.assertEqual(delivered, [("second", SUMMARY)])
            self.assertIn("Summary reply failed", output.getvalue())
            self.assertEqual(report.read_text(), SUMMARY)


if __name__ == "__main__":
    unittest.main()
