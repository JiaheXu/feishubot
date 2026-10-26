import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from paper_analyzer import PaperAnalyzer
import summary_messages


class MessageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.messages = self.root / "messages.json"
        self.messages.write_text(json.dumps([
            {"message_id": "test", "content": "https://example.org/paper"},
        ]))
        self.analyzer = PaperAnalyzer(papers_dir=self.root / "papers")
        discovery = patch("paper_analyzer.discover", return_value=([
            {"url": "https://example.org/paper", "text": "Score 42 versus baseline 30."},
        ], "Source verified."))
        self.discovery = discovery.start()
        self.addCleanup(discovery.stop)

    def run_messages(self):
        output = io.StringIO()
        with patch("summary_messages.PaperAnalyzer.from_env", return_value=self.analyzer), redirect_stdout(output):
            status = summary_messages.main([
                str(self.messages), "--output-dir", str(self.root / "reports"),
            ])
        return status, output.getvalue()

    def test_generates_and_prints_a_short_message(self):
        summary = "Robot learning\n• 问题：适应新任务。\n• 创新：更新程序。\n• 方法：从试错中积累技能。\n• 结果：42 对比 30。\nhttps://example.org/paper"
        summary += "\n• 局限性（AI 分析）：真机泛化尚未验证。"
        with patch("paper_analyzer.archive_papers", return_value=([], [])), \
                patch.object(self.analyzer, "_chat", return_value={"content": summary}):
            status, output = self.run_messages()
        self.assertEqual(status, 0)
        self.assertIn(summary, output)
        self.assertEqual(next((self.root / "reports").glob("*.md")).read_text(), "本摘要由 gpt-6-astra 生成\n" + summary + "\n")

    def test_failed_message_sets_nonzero_status_and_continues(self):
        self.messages.write_text(json.dumps([
            {"content": "https://example.org/one"}, {"content": "https://example.org/two"},
        ]))
        self.discovery.side_effect = RuntimeError("unreadable")
        with patch("paper_analyzer.archive_papers", return_value=([], [])):
            status, output = self.run_messages()
        self.assertEqual(status, 1)
        self.assertEqual(output.count("Analysis unavailable:"), 2)
        self.assertEqual(len(list((self.root / "reports").glob("*.md"))), 2)

    def test_summary_missing_required_chinese_sections_fails_the_message_check(self):
        with patch("paper_analyzer.archive_papers", return_value=([], [])), \
                patch.object(self.analyzer, "_chat", return_value={"content": "Only an English overview."}):
            status, output = self.run_messages()
        self.assertEqual(status, 1)
        self.assertIn("FAIL:", output)

    def test_summary_without_ai_attributed_limitations_fails_the_message_check(self):
        summary = "问题：任务。创新：技能。方法：探索。结果：42。\n局限性：仅仿真。"
        with patch("paper_analyzer.archive_papers", return_value=([], [])), \
                patch.object(self.analyzer, "_chat", return_value={"content": summary}):
            status, output = self.run_messages()
        self.assertEqual(status, 1)
        self.assertIn("FAIL:", output)

    def test_nonresearch_message_is_skipped_without_a_failure_report(self):
        self.discovery.return_value = ([], "not_research: ordinary chat")
        status, output = self.run_messages()
        self.assertEqual(status, 0)
        self.assertIn("SKIP:", output)
        self.assertNotIn("FAIL:", output)
        self.assertFalse((self.root / "reports").exists())

    def test_summary_with_many_short_technical_terms_passes(self):
        summary = "问题：适应新任务。创新：积累技能。方法：在线探索。结果：42 对比 30。\n" + "RL " * 121
        summary += "\n局限性（AI 分析）：仅仿真。"
        with patch("paper_analyzer.archive_papers", return_value=([], [])), \
                patch.object(self.analyzer, "_chat", return_value={"content": summary}):
            status, output = self.run_messages()
        self.assertEqual(status, 0)
        self.assertIn(summary.strip(), output)

    def test_invalid_or_empty_inputs_are_rejected_before_configuration(self):
        for data in ([], {}, [None], [{"content": " "}], [{"content": 1}]):
            self.messages.write_text(json.dumps(data))
            with self.subTest(data=data), patch("summary_messages.PaperAnalyzer.from_env") as configure:
                with self.assertRaises(ValueError):
                    summary_messages.main([str(self.messages)])
                configure.assert_not_called()

    def test_persistently_oversized_ai_output_fails_the_message_check(self):
        for text in ("word " * 201, "x" * 1001):
            with self.subTest(text_length=len(text)), \
                    patch("paper_analyzer.archive_papers", return_value=([], [])), \
                    patch.object(self.analyzer, "_chat", return_value={"content": text}):
                status, output = self.run_messages()
            self.assertEqual(status, 1)
            self.assertIn("Summary still exceeds the single-message limit", output)


if __name__ == "__main__":
    unittest.main()
