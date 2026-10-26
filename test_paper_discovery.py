import io
from contextlib import redirect_stdout
import json
from pathlib import Path
import tempfile
import os
import unittest
from unittest.mock import patch

from paper_analyzer import PaperAnalyzer
import paper_sources
import monitor


def call(name, arguments, identifier):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": identifier, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }]}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.analyzer = PaperAnalyzer(output_dir=self.root)
        self.source = {"url": "https://example.org/paper", "text": "Title: Robot Learning. Results: 42.",
                       "retrieved_url": "https://example.org/paper", "truncated": False}

    def test_title_only_message_searches_reads_verifies_then_summarizes(self):
        replies = [call("search_web", {"query": "Robot Learning paper"}, "search"),
                   call("read_source", {"url": self.source["url"]}, "read"),
                   {"role": "assistant", "content": json.dumps({"status": "verified",
                    "reason": "Title and method match the message.", "urls": [self.source["url"]]})}]
        with patch.object(self.analyzer, "_chat", side_effect=replies), \
                patch("paper_discovery.search_web", return_value=[{"title": "Robot Learning", "url": self.source["url"]}]), \
                patch("paper_discovery.fetch_source", return_value=self.source), \
                patch.object(self.analyzer, "_complete", return_value="Summary grounded in result 42.") as summarize:
            path = self.analyzer.analyze({"message_id": "title", "content": "Robot Learning paper"})
        self.assertIn("result 42", path.read_text())
        self.assertIn("Title and method match", path.with_suffix(".json").read_text())
        self.assertEqual(summarize.call_args.args[0], [self.source])

    def test_pdf_with_test_caption_is_read_before_classification(self):
        url = "https://research.nvidia.com/labs/gear/aspire/assets/aspire.pdf?v=20261016"
        row = [{"tag": "a", "href": url, "text": url}, {"tag": "text", "text": " test msg"}]
        content = json.dumps({"title": "", "content": [row], "content_v2": [row]})
        source = {"url": url, "retrieved_url": url,
                  "text": "ASPIRE: A Robotics Research Paper. Abstract: A new method. Experiments: 42 versus 30.",
                  "truncated": False}
        summary = "ASPIRE\n• 问题：适应新任务。\n• 创新：新方法。\n• 方法：在线优化。\n• 结果：42 对比 30。\n" + url
        requests = []

        def chat(messages, tools=None):
            requests.append(messages)
            if len(requests) == 1:
                self.assertIn(source["text"], messages[1]["content"])
                return {"role": "assistant", "content": json.dumps({
                    "status": "verified", "reason": "The linked PDF is a paper despite its test caption.",
                    "urls": [url]})}
            return {"role": "assistant", "content": summary}

        with patch("paper_discovery.fetch_source", return_value=source) as fetch, \
                patch("paper_analyzer.archive_papers", return_value=([], [])), \
                patch.object(self.analyzer, "_chat", side_effect=chat):
            path = self.analyzer.analyze({"message_id": "aspire-test", "content": content})
        self.assertIsNotNone(path)
        self.assertIn("• 结果：", path.read_text())
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(len(requests), 2)

    def test_prefetch_and_agent_reads_share_source_and_text_limits(self):
        from paper_discovery import discover

        for count, size, expected_fetches, error in ((11, 20, 10, "10 links"), (4, 100_000, 3, "budget exhausted")):
            urls = [f"https://example.org/paper-{index}" for index in range(count)]
            replies = [call("read_source", {"url": "https://example.org/additional-paper"}, "extra"),
                       {"role": "assistant", "content": json.dumps({
                           "status": "verified", "reason": "First paper matches.", "urls": [urls[0]]})}]
            with self.subTest(count=count, size=size), \
                    patch("paper_discovery.fetch_source", side_effect=lambda url: {"url": url, "text": "x" * size}) as fetch, \
                    patch.object(self.analyzer, "_chat", side_effect=replies) as chat:
                sources, reason = discover(self.analyzer, "\n".join(urls))
            self.assertEqual(fetch.call_count, expected_fetches)
            self.assertEqual(sources[0]["url"], urls[0])
            tool_result = next(message["content"] for message in chat.call_args.args[0] if message["role"] == "tool")
            self.assertIn(error, json.loads(tool_result)["error"])

    def test_project_href_leads_to_its_paper_before_classification(self):
        home_url = "https://example.org/project/"
        paper_url = "https://example.org/project/paper.pdf"
        homepage = {"url": home_url, "text": "Project homepage with a paper button.", "paper_urls": [paper_url]}
        paper = {"url": paper_url, "text": "Robot Learning paper. Method and experiments: 42 versus 30."}

        def chat(messages, tools=None):
            self.assertIn(homepage["text"], messages[1]["content"])
            self.assertIn(paper["text"], messages[1]["content"])
            return {"role": "assistant", "content": json.dumps({
                "status": "verified", "reason": "Read the project's main paper.", "urls": [paper_url]})}

        content = json.dumps({"content": [[{"tag": "a", "href": home_url, "text": "project homepage"}]]})
        with patch("paper_discovery.fetch_source", side_effect=[homepage, paper]) as fetch, \
                patch.object(self.analyzer, "_chat", side_effect=chat), \
                patch("paper_analyzer.archive_papers", return_value=([], [])), \
                patch.object(self.analyzer, "_complete", return_value="Paper result: 42 versus 30.") as summarize:
            path = self.analyzer.analyze({"message_id": "project-href", "content": content})
        self.assertIn("42 versus 30", path.read_text())
        self.assertEqual(summarize.call_args.args[0], [paper])
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [home_url, paper_url])

    def test_broken_project_paper_link_keeps_the_readable_homepage(self):
        from paper_discovery import discover

        home_url = "https://example.org/project/"
        paper_url = "https://example.org/project/paper.pdf"
        homepage = {"url": home_url, "text": "Research method and results.", "pdf_url": paper_url}

        def chat(messages, tools=None):
            self.assertIn("HTTP 404", messages[1]["content"])
            return {"role": "assistant", "content": json.dumps({
                "status": "verified", "reason": "Project report readable; PDF unavailable.", "urls": [home_url]})}

        with patch("paper_discovery.fetch_source", side_effect=[homepage, RuntimeError("HTTP 404")]), \
                patch.object(self.analyzer, "_chat", side_effect=chat):
            sources, reason = discover(self.analyzer, home_url)
        self.assertEqual(sources, [homepage])

    def test_direct_link_also_runs_discovery_before_summary(self):
        replies = [call("read_source", {"url": self.source["url"]}, "read"),
                   {"role": "assistant", "content": json.dumps({"status": "verified",
                    "reason": "Source title and method checked.", "urls": [self.source["url"]]})},
                   {"role": "assistant", "content": "机器人学习\n• 问题：如何适应新任务。\n• 创新：持续积累技能。\n• 方法：探索并编写程序。\n• 结果：42 对比 30。\nhttps://example.org/paper"}]
        with patch.object(self.analyzer, "_chat", side_effect=replies) as chat, \
                patch("paper_discovery.fetch_source", return_value=self.source), \
                patch("paper_analyzer.archive_papers", return_value=([], [])):
            path = self.analyzer.analyze({"message_id": "linked", "content": self.source["url"]})
        self.assertIn("• 问题：", path.read_text())
        self.assertEqual(chat.call_count, 3)
        summary_payload = json.loads(chat.call_args.args[0][1]["content"])
        self.assertEqual(summary_payload["paper_urls"], [self.source["url"]])
        self.assertEqual(summary_payload["sources"][0]["text"], self.source["text"])

    def test_direct_link_rejected_by_discovery_does_not_trigger_summary(self):
        reply = {"role": "assistant", "content":
                 '{"status":"not_research","reason":"Not a research source","urls":[]}'}
        with patch.object(self.analyzer, "_chat", return_value=reply), \
                patch("paper_discovery.fetch_source", return_value={
                    "url": self.source["url"], "text": "Personal landing page; no research report."}), \
                patch("paper_analyzer.archive_papers", return_value=([], [])), \
                patch.object(self.analyzer, "_complete") as summarize:
            path = self.analyzer.analyze({"message_id": "not-research", "content": self.source["url"]})
        summarize.assert_not_called()
        self.assertIsNone(path)

    def test_agent_cannot_verify_a_url_it_did_not_read(self):
        reply = {"role": "assistant", "content": json.dumps({"status": "verified",
                 "reason": "I remember it", "urls": [self.source["url"]]})}
        with patch.object(self.analyzer, "_chat", return_value=reply), \
                patch.object(self.analyzer, "_complete") as summarize:
            path = self.analyzer.analyze({"message_id": "unread", "content": "Robot Learning paper"})
        summarize.assert_not_called()
        self.assertIn("not retrieved", path.with_suffix(".json").read_text())

    def test_ambiguous_or_irrelevant_messages_are_not_summarized(self):
        for status in ("ambiguous", "not_research", "unavailable"):
            with self.subTest(status=status), patch.object(self.analyzer, "_chat", return_value={
                    "role": "assistant", "content": json.dumps({"status": status, "reason": "No unique match", "urls": []})}), \
                    patch.object(self.analyzer, "_complete") as summarize:
                path = self.analyzer.analyze({"message_id": status, "content": "test"})
            summarize.assert_not_called()
            if status == "not_research":
                self.assertIsNone(path)
            else:
                self.assertIn(status, path.with_suffix(".json").read_text())

    def test_tool_loop_is_bounded(self):
        with patch.object(self.analyzer, "_chat", return_value=call("search_web", {"query": "paper"}, "search")), \
                patch("paper_discovery.search_web", return_value=[]), \
                patch.object(self.analyzer, "_complete") as summarize:
            path = self.analyzer.analyze({"content": "Robot Learning paper"})
        summarize.assert_not_called()
        self.assertIn("limit", path.with_suffix(".json").read_text())

    def test_discovery_prompt_is_reloaded(self):
        prompt = self.root / "discovery.txt"
        prompt.write_text("First discovery instructions")
        self.analyzer.discovery_prompt_path = prompt
        reply = {"role": "assistant", "content": '{"status":"not_research","reason":"chatter","urls":[]}'}
        seen = []
        def chat(messages, tools=None):
            seen.append(messages[0]["content"])
            return reply
        with patch.object(self.analyzer, "_chat", side_effect=chat):
            self.analyzer.analyze({"content": "hello"})
            prompt.write_text("Updated discovery instructions")
            self.analyzer.analyze({"content": "hello again"})
        self.assertEqual(seen, ["First discovery instructions", "Updated discovery instructions"])

    def test_search_extracts_result_links_and_rejects_challenge_pages(self):
        html = b'<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fpaper">Robot <b>Learning</b></a>'
        with patch("paper_sources.download", return_value=(html, "text/html", "https://html.duckduckgo.com/html/")):
            results = paper_sources.search_web("Robot Learning")
        self.assertEqual(results, [{"title": "Robot Learning", "url": "https://example.org/paper"}])
        with patch("paper_sources.download", return_value=(b"captcha", "text/html", "https://html.duckduckgo.com")):
            with self.assertRaisesRegex(RuntimeError, "search results"):
                paper_sources.search_web("Robot Learning")

    def test_arxiv_search_returns_candidate_metadata_for_verification(self):
        atom = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <id>http://arxiv.org/abs/2610.22496v1</id><title>Scaling Online Exploration\nfor Robot Agents</title>
        <summary>Robots update skills through online exploration.</summary><author><name>Author</name></author>
        </entry></feed>'''
        with patch("paper_sources.download", return_value=(atom, "application/atom+xml", "https://export.arxiv.org")):
            results = paper_sources.search_papers('ti:"Scaling Online Exploration for Robot Agents"')
        self.assertEqual(results[0]["url"], "https://arxiv.org/abs/2610.22496v1")
        self.assertEqual(results[0]["title"], "Scaling Online Exploration for Robot Agents")
        self.assertEqual(results[0]["authors"], ["Author"])

    def test_paper_search_falls_back_when_arxiv_is_rate_limited(self):
        payload = json.dumps([{"paper": {
            "id": "2610.22496", "title": "Scaling Online Exploration for Robot Agents",
            "summary": "Online exploration compiles into robot programs.",
            "authors": [{"name": "Runhan Huang"}],
        }}]).encode()
        with patch("paper_sources.download", side_effect=[
                RuntimeError("Source returned HTTP 429."),
                (payload, "application/json", "https://huggingface.co/api/papers/search"),
        ]) as download:
            results = paper_sources.search_papers('ti:"Scaling Online Exploration for Robot Agents"')
        self.assertEqual(results, [{
            "title": "Scaling Online Exploration for Robot Agents",
            "url": "https://arxiv.org/abs/2610.22496",
            "abstract": "Online exploration compiles into robot programs.",
            "authors": ["Runhan Huang"],
        }])
        self.assertIn("huggingface.co/api/papers/search?", download.call_args.args[0])
        self.assertIn("q=Scaling+Online+Exploration+for+Robot+Agents", download.call_args.args[0])

    def test_paper_search_reports_when_both_providers_fail(self):
        with patch("paper_sources.download", side_effect=[
                RuntimeError("HTTP 429"), RuntimeError("HTTP 503"),
        ]):
            with self.assertRaisesRegex(RuntimeError, "429.*503"):
                paper_sources.search_papers('ti:"Robot Learning"')

    def test_replay_prints_summary_without_feishu_credentials(self):
        fixture = self.root / "messages.json"
        fixture.write_text('[{"message_id":"history","content":"Robot Learning paper"}]')
        report = self.root / "report.md"
        report.write_text("Local summary from real-message fixture")
        output = io.StringIO()
        with patch("sys.argv", ["monitor.py", "--replay", str(fixture)]), \
                patch.dict(os.environ, {}, clear=True), \
                patch("paper_analyzer.PaperAnalyzer.from_env", return_value=self.analyzer), \
                patch.object(self.analyzer, "analyze", return_value=report) as analyze, redirect_stdout(output):
            monitor.main()
        self.assertIn("Local summary from real-message fixture", output.getvalue())
        self.assertEqual(analyze.call_args.args[0]["message_id"], "history")

    def test_replay_skips_nonresearch_messages_without_reading_a_report(self):
        fixture = self.root / "messages.json"
        fixture.write_text('[{"content":"hello"}]')
        output = io.StringIO()
        with patch("sys.argv", ["monitor.py", "--replay", str(fixture)]), \
                patch("paper_analyzer.PaperAnalyzer.from_env", return_value=self.analyzer), \
                patch.object(self.analyzer, "analyze", return_value=None), redirect_stdout(output):
            monitor.main()
        self.assertIn("Skipped", output.getvalue())
        self.assertNotIn("Analysis report saved", output.getvalue())

    def test_replay_rejects_malformed_messages_before_any_ai_call(self):
        fixture = self.root / "invalid.json"
        fixture.write_text('[{"content":42}]')
        with patch("sys.argv", ["monitor.py", "--replay", str(fixture)]), \
                patch("paper_analyzer.PaperAnalyzer.from_env") as configure:
            with self.assertRaisesRegex(ValueError, "nonempty string content"):
                monitor.main()
        configure.assert_not_called()

    def test_chat_preserves_function_calls_for_the_discovery_loop(self):
        reply = call("search_papers", {"query": 'ti:"Robot Learning"'}, "search")
        response = io.BytesIO(json.dumps({"choices": [{"message": reply, "finish_reason": "tool_calls"}]}).encode())
        from paper_discovery import TOOLS
        with patch("paper_analyzer.urllib.request.OpenerDirector.open", return_value=response) as send:
            actual = self.analyzer._chat([{"role": "user", "content": "Robot Learning"}], tools=TOOLS)
        self.assertEqual(actual, reply)
        payload = json.loads(send.call_args.args[0].data)
        self.assertEqual(payload["tools"][0]["function"]["name"], "search_papers")
