import io
from contextlib import redirect_stderr
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error

import paper_analyzer
import paper_sources


class SourceTests(unittest.TestCase):
    def test_extracts_unique_links_from_text_and_rich_posts(self):
        content = json.dumps({"content": [[
            {"tag": "a", "href": "https://arxiv.org/abs/2401.00001"},
            {"tag": "text", "text": "See https://example.org/paper.pdf。 https://arxiv.org/abs/2401.00001"},
        ]]})
        self.assertEqual(paper_sources.extract_links(content), [
            "https://arxiv.org/abs/2401.00001", "https://example.org/paper.pdf",
        ])

    def test_plain_messages_without_links_have_no_direct_urls(self):
        self.assertEqual(paper_sources.extract_links("分享一篇论文"), [])

    def test_private_urls_and_credentials_are_rejected(self):
        for url in ("file:///etc/passwd", "http://127.0.0.1/", "http://[::1]/",
                    "http://169.254.169.254/", "http://user:pass@example.org/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                paper_sources.public_target(url)

    def test_host_resolving_to_private_address_is_rejected(self):
        with patch("paper_sources.socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ]), self.assertRaises(ValueError):
            paper_sources.public_target("https://example.org/paper")

    def test_public_ip_is_pinned_for_download(self):
        parsed, address = paper_sources.public_target("https://8.8.8.8/paper")
        self.assertEqual(parsed.hostname, "8.8.8.8")
        self.assertEqual(address, "8.8.8.8")

    def test_redirect_to_localhost_is_blocked_before_second_request(self):
        with patch("paper_sources._request", return_value=(302, {"location": "http://127.0.0.1/private"}, b"")) as request:
            with self.assertRaises(ValueError):
                paper_sources.download("https://8.8.8.8/paper")
        self.assertEqual(request.call_count, 1)

    def test_download_proxy_tunnels_to_validated_public_ip(self):
        parsed, address = paper_sources.public_target("https://8.8.8.8/paper")
        with patch("paper_sources.http.client.HTTPConnection") as connection:
            tunnel = connection.return_value
            expected_socket = tunnel.sock
            result = paper_sources._source_socket(parsed, address, "http://127.0.0.1:7890")
        self.assertIs(result, expected_socket)
        connection.assert_called_once_with("127.0.0.1", 7890, timeout=30)
        tunnel.set_tunnel.assert_called_once_with("8.8.8.8", 443, headers={})

    def test_direct_download_uses_validated_ip_without_resolving_again(self):
        parsed, address = paper_sources.public_target("https://8.8.8.8/paper")
        with patch("paper_sources.socket.create_connection") as connect:
            paper_sources._source_socket(parsed, address, None)
        connect.assert_called_once_with(("8.8.8.8", 443), timeout=30)

    def test_html_extraction_omits_scripts_and_navigation(self):
        html = b'<html><title>Paper title</title><nav>Menu</nav><script>ignore()</script><article>Problem solved. Result: 42.</article></html>'
        with patch("paper_sources.download", return_value=(html, "text/html", "https://example.org/paper")):
            source = paper_sources.fetch_source("https://example.org/paper")
        self.assertIn("Problem solved. Result: 42.", source["text"])
        self.assertNotIn("ignore", source["text"])
        self.assertNotIn("Menu", source["text"])

    def test_arxiv_tries_full_text_before_abstract(self):
        with patch("paper_sources.download", return_value=(b"<article>Full paper</article>", "text/html", "https://arxiv.org/html/2401.00001")) as download:
            source = paper_sources.fetch_source("https://arxiv.org/abs/2401.00001")
        self.assertEqual(download.call_args.args[0], "https://arxiv.org/html/2401.00001")
        self.assertEqual(source["text"], "Full paper")

    def test_empty_page_is_reported_as_unreadable(self):
        with patch("paper_sources.download", return_value=(b"<script>only JS</script>", "text/html", "https://example.org")):
            with self.assertRaises(RuntimeError):
                paper_sources.fetch_source("https://example.org")

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "Install requirements.txt for PDF extraction test")
    def test_extracts_actual_pdf_text_with_page_reference(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        writer = PdfWriter()
        page = writer.add_blank_page(300, 300)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 10 200 Td (Test paper result: 42) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        data = io.BytesIO()
        writer.write(data)
        text, truncated = paper_sources._extract(data.getvalue(), "application/pdf")
        self.assertIn("[Page 1]", text)
        self.assertIn("Test paper result: 42", text)
        self.assertFalse(truncated)

    def test_long_source_is_explicitly_marked_as_truncated(self):
        with patch("paper_sources.MAX_TEXT", 10):
            text, truncated = paper_sources._extract(b"<article>0123456789 extra</article>", "text/html")
        self.assertEqual(text, "0123456789")
        self.assertTrue(truncated)


class AnalyzerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("Explain the setting and results.", encoding="utf-8")
        self.analyzer = paper_analyzer.PaperAnalyzer(
            endpoint="http://127.0.0.1:8787/v1/chat/completions",
            api_key="dummy", prompt_path=self.prompt, output_dir=self.root / "analyses", papers_dir=self.root / "papers",
        )
        self.message = {"message_id": "om_test", "content": '{"text":"https://example.org/paper"}'}
        self.source = {"url": "https://example.org/paper", "retrieved_url": "https://example.org/paper",
                       "text": "Experiment: task X, score 42 versus baseline 30.", "truncated": False}
        discovery = patch("paper_analyzer.discover", return_value=([self.source], "Source verified."))
        self.discovery = discovery.start()
        self.addCleanup(discovery.stop)

    @staticmethod
    def response(text="## Results\nScore 42."):
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}).encode())

    def test_sends_retrieved_content_to_astra_and_saves_report(self):
        with patch("paper_analyzer.urllib.request.OpenerDirector.open", return_value=self.response()) as send:
            report = self.analyzer.analyze(self.message)
        request = send.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "http://127.0.0.1:8787/v1/chat/completions")
        self.assertEqual(payload["model"], "gpt-6-astra")
        self.assertIn("task X, score 42", payload["messages"][1]["content"])
        self.assertEqual(payload["messages"][0]["content"], "Explain the setting and results.")
        self.assertIn("## Results", report.read_text())
        self.assertIn("https://example.org/paper", report.with_suffix(".json").read_text())
        self.assertEqual(report.read_text(), "本摘要由 gpt-6-astra 生成\n## Results\nScore 42.\n")
        self.assertNotIn("Source coverage", report.read_text())
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)

    def test_prompt_is_reloaded_without_changing_code(self):
        with patch("paper_analyzer.urllib.request.OpenerDirector.open", side_effect=lambda *a, **k: self.response()) as send:
            self.analyzer.analyze(self.message)
            self.prompt.write_text("New instructions in Chinese.", encoding="utf-8")
            self.analyzer.analyze(self.message)
        payload = json.loads(send.call_args.args[0].data)
        self.assertEqual(payload["messages"][0]["content"], "New instructions in Chinese.")

    def test_disclosure_uses_proxy_model_and_replaces_generated_attribution(self):
        for response_model, expected in (("claude-sonnet-test", "claude-sonnet-test"),
                                         (None, "local-model-alias")):
            self.analyzer.model = "local-model-alias"
            data = {"choices": [{"message": {"content": "本摘要由 CHATGPT 生成\n论文内容。"},
                                  "finish_reason": "stop"}]}
            if response_model:
                data["model"] = response_model
            with self.subTest(model=response_model), patch(
                    "paper_analyzer.urllib.request.OpenerDirector.open",
                    return_value=io.BytesIO(json.dumps(data).encode())):
                report = self.analyzer.analyze(self.message)
            self.assertEqual(report.read_text(), f"本摘要由 {expected} 生成\n论文内容。\n")

    def test_reference_script_environment_names_are_supported(self):
        analyzer = paper_analyzer.PaperAnalyzer.from_env({
            "CODEX_PROXY_BASE_URL": "http://127.0.0.1:8787/v1/",
            "CODEX_PROXY_API_KEY": "dummy-key", "OPENAI_MODEL": "custom-astra-alias",
        })
        self.assertEqual(analyzer.endpoint, "http://127.0.0.1:8787/v1/chat/completions")
        self.assertEqual(analyzer.model, "custom-astra-alias")
        self.assertEqual(analyzer.api_key, "dummy-key")

    def test_ai_specific_endpoint_takes_precedence(self):
        analyzer = paper_analyzer.PaperAnalyzer.from_env({
            "AI_BASE_URL": "http://127.0.0.1:8787/v1/chat/completions",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
            "AI_MODEL": "gpt-6-astra", "AI_API_KEY": "dummy",
        })
        self.assertEqual(analyzer.endpoint, "http://127.0.0.1:8787/v1/chat/completions")

    def test_empty_and_truncated_responses_are_not_presented_as_complete(self):
        for choice in ({"message": {"content": ""}, "finish_reason": "stop"},
                       {"message": {"content": "partial"}, "finish_reason": "length"}):
            response = io.BytesIO(json.dumps({"choices": [choice]}).encode())
            with self.subTest(choice=choice), patch("paper_analyzer.urllib.request.OpenerDirector.open", return_value=response):
                report = self.analyzer.analyze(self.message)
            self.assertIn("Analysis unavailable", report.read_text())

    def test_unreadable_sources_do_not_trigger_invented_analysis(self):
        self.discovery.return_value = ([{"url": self.source["url"], "error": "HTTP 403"}], "HTTP 403")
        with patch("paper_analyzer.urllib.request.OpenerDirector.open") as send:
            report = self.analyzer.analyze(self.message)
        send.assert_not_called()
        self.assertIn("Analysis unavailable", report.read_text())
        self.assertIn("HTTP 403", report.with_suffix(".json").read_text())

    def test_oversized_summary_is_shortened_once_instead_of_printing_a_long_report(self):
        with patch.object(self.analyzer, "_chat", side_effect=[
                {"content": "x" * 1001}, {"content": "New idea: compact summary. Results: 42."}]) as chat:
            summary = self.analyzer._complete([self.source], "paper")
        self.assertLessEqual(len(summary), 1000)
        self.assertEqual(chat.call_count, 2)

    def test_summary_at_1000_character_boundary_is_not_shortened(self):
        text = "本摘要由 gpt-6-astra 生成\n"
        text += "中" * (1000 - len(text))
        with patch.object(self.analyzer, "_chat", return_value={"content": text}) as chat:
            summary = self.analyzer._complete([self.source], "paper")
        self.assertEqual(summary, text)
        self.assertEqual(chat.call_count, 1)

    def test_technical_terms_and_numbers_do_not_trigger_a_word_limit(self):
        text = "RL " * 121
        with patch.object(self.analyzer, "_chat", return_value={"content": text}) as chat:
            summary = self.analyzer._complete([self.source], "paper")
        self.assertEqual(summary, "本摘要由 gpt-6-astra 生成\n" + text.strip())
        self.assertEqual(chat.call_count, 1)

    def test_pdf_failure_does_not_prevent_summary_and_is_recorded(self):
        with patch("paper_analyzer.archive_papers", return_value=([], ["PDF download failed"])), \
                patch.object(self.analyzer, "_complete", return_value="Idea: X. Results: 42."), redirect_stderr(io.StringIO()):
            report = self.analyzer.analyze(self.message)
        self.assertEqual(report.read_text(), "Idea: X. Results: 42.\n")
        self.assertIn("PDF download failed", report.with_suffix(".json").read_text())

    def test_unreadable_pdf_is_still_sent_to_archiving(self):
        message = {"message_id": "scanned", "content": "https://example.org/scanned.pdf"}
        self.discovery.return_value = ([{"url": message["content"], "error": "PDF has no extractable text"}], "unreadable")
        with patch("paper_analyzer.archive_papers", return_value=(["papers/scanned.pdf"], [])) as archive, \
                patch.object(self.analyzer, "_complete") as summarize:
            path = self.analyzer.analyze(message)
        summarize.assert_not_called()
        self.assertEqual(archive.call_args.args[0]["url"], message["content"])
        self.assertIn("papers/scanned.pdf", path.with_suffix(".json").read_text())

    def test_proxy_error_is_saved_without_authentication_data(self):
        error = urllib.error.HTTPError("http://127.0.0.1:8787/v1/chat/completions", 401, "Unauthorized", {}, io.BytesIO(b"secret response"))
        with patch("paper_analyzer.urllib.request.OpenerDirector.open", side_effect=error):
            report = self.analyzer.analyze(self.message)
        text = report.read_text()
        self.assertIn("401", text)
        self.assertNotIn("dummy", text)
        self.assertNotIn("secret response", text)

    def test_worker_does_not_block_message_handler_and_survives_failure(self):
        started, release = threading.Event(), threading.Event()
        messages = []
        saved_report = self.root / "dummy-report.md"
        saved_report.write_text("Local summary text", encoding="utf-8")

        class Analyzer:
            def analyze(self, message):
                messages.append(message["message_id"])
                if len(messages) == 1:
                    started.set()
                    release.wait(2)
                    raise RuntimeError("test failure")
                return saved_report

        output = io.StringIO()
        worker = paper_analyzer.AnalysisWorker(Analyzer(), output)
        try:
            self.assertFalse(worker.submit({"content": ""}))
            self.assertTrue(worker.submit(self.message))
            self.assertTrue(started.wait(1))
            self.assertTrue(worker.submit({**self.message, "message_id": "om_second",
                                           "content": "Robot Learning paper"}))
        finally:
            release.set()
            worker.close()
        self.assertEqual(messages, ["om_test", "om_second"])
        self.assertIn("dummy-report.md", output.getvalue())
        self.assertIn("Local summary text", output.getvalue())


class LauncherTests(unittest.TestCase):
    def test_launch_uses_its_directory_and_exports_only_dummy_local_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(Path(__file__).with_name("launch.sh"), root / "launch.sh")
            (root / "local_env.sh").write_text(
                "PAPER_APP_ID=cli_dummy\nPAPER_APP_SECRET=dummy-secret\n"
                "CODEX_PROXY_API_KEY=dummy-key\nLOCAL_HTTP_PROXY=http://127.0.0.1:9999\n")
            interpreter = root / "fake-python"
            interpreter.write_text(
                "#!/usr/bin/python3\nimport json, os, sys\n"
                "print(json.dumps({'cwd':os.getcwd(), 'args':sys.argv[1:], "
                "'id':os.environ.get('PAPER_APP_ID'), 'model':os.environ.get('AI_MODEL'), "
                "'endpoint':os.environ.get('AI_BASE_URL'), 'key':os.environ.get('AI_API_KEY'), "
                "'no_proxy':os.environ.get('NO_PROXY')}))\n")
            interpreter.chmod(0o700)
            result = subprocess.run(["bash", str(root / "launch.sh"), "--group", "paper分享"],
                                    cwd="/tmp", env={"PATH": os.defpath, "PYTHON_BIN": str(interpreter)},
                                    text=True, capture_output=True, check=True)
            data = json.loads(result.stdout)
            self.assertEqual(data["cwd"], directory)
            self.assertEqual(data["args"], [str(root / "monitor.py"), "--group", "paper分享"])
            self.assertEqual(data["id"], "cli_dummy")
            self.assertEqual(data["model"], "gpt-6-astra")
            self.assertEqual(data["endpoint"], "http://127.0.0.1:8787/v1/chat/completions")
            self.assertEqual(data["key"], "dummy-key")
            self.assertIn("127.0.0.1", data["no_proxy"])


if __name__ == "__main__":
    unittest.main()
