import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import queue
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import monitor


class CredentialsTests(unittest.TestCase):
    def test_existing_parameter_names_can_hold_app_credentials(self):
        self.assertEqual(
            monitor.credentials({"PAPER_BOT_URL": "cli_test", "PAPER_BOT_KEY": "dummy"}),
            ("cli_test", "dummy"),
        )

    def test_webhook_credentials_report_receive_limitation_without_secrets(self):
        with self.assertRaises(ValueError) as caught:
            monitor.credentials({
                "PAPER_BOT_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/private",
                "PAPER_BOT_KEY": "private-key",
            })
        self.assertIn("cannot receive", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))

    def test_app_credentials_override_webhook_parameters(self):
        self.assertEqual(monitor.credentials({
            "PAPER_BOT_URL": "https://example.invalid/webhook",
            "PAPER_BOT_KEY": "unused-dummy",
            "PAPER_APP_ID": "cli_test",
            "PAPER_APP_SECRET": "app-dummy",
        }), ("cli_test", "app-dummy"))

    def test_missing_or_partial_credentials_fail(self):
        for env in ({}, {"PAPER_APP_ID": "cli_test"}, {"PAPER_APP_SECRET": "dummy"}):
            with self.subTest(env=env), self.assertRaises(ValueError):
                monitor.credentials(env)


class GroupTests(unittest.TestCase):
    def test_inactive_app_error_explains_installation_and_activation(self):
        message = monitor.chat_list_error(232034)
        self.assertIn("232034", message)
        self.assertIn("not installed or enabled", message)
        self.assertIn("版本管理与发布", message)
        self.assertNotIn("check app permissions", message)

    def test_other_api_errors_keep_their_code(self):
        self.assertIn("99991672", monitor.chat_list_error(99991672))

    def test_exact_name_on_later_page(self):
        pages = {
            None: ([{"name": "paper分享其他", "chat_id": "oc_other"}], "next"),
            "next": ([{"name": "paper分享", "chat_id": "oc_paper"}], None),
        }
        self.assertEqual(monitor.find_chat("paper分享", pages.__getitem__), "oc_paper")

    def test_missing_or_duplicate_name_fails(self):
        for items in ([], [
            {"name": "paper分享", "chat_id": "oc_1"},
            {"name": "paper分享", "chat_id": "oc_2"},
        ]):
            with self.subTest(items=items), self.assertRaises(ValueError):
                monitor.find_chat("paper分享", lambda token: (items, None))


class MonitorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.log_path = Path(directory.name) / "messages.txt"
        self.output = io.StringIO()
        self.listener = monitor.MessageMonitor("oc_paper", self.output, self.log_path)
        self.event = {
            "header": {"event_id": "event-1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_dummy"}},
                "message": {
                    "chat_id": "oc_paper", "chat_type": "group",
                    "message_id": "om_1", "message_type": "text",
                    "content": '{"text":"论文分享"}', "create_time": "1000",
                },
            },
        }

    def test_prints_and_saves_unmentioned_text_without_json_wrapper(self):
        self.listener.handle(self.event)
        self.assertEqual(self.output.getvalue(), "论文分享\n")
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), "论文分享\n")

    def test_monitor_queues_only_new_live_messages(self):
        jobs = queue.SimpleQueue()
        self.listener = monitor.MessageMonitor(
            "oc_paper", self.output, self.log_path, worker=SimpleNamespace(submit=jobs.put))
        history = copy.deepcopy(self.event)
        history["event"]["message"]["message_id"] = "om_history"
        self.listener.handle(history, history=True)
        other = copy.deepcopy(self.event)
        other["event"]["message"]["chat_id"] = "oc_other"
        self.listener.handle(other)
        bot = copy.deepcopy(self.event)
        bot["event"]["message"]["message_id"] = "om_bot"
        bot["event"]["sender"]["sender_type"] = "app"
        self.listener.handle(bot)
        self.listener.handle(self.event)
        self.listener.handle(self.event)
        self.assertEqual(jobs.qsize(), 1)
        self.assertEqual(jobs.get_nowait(), self.event["event"]["message"])
        self.assertEqual(self.log_path.read_text().count("论文分享"), 3)

    def test_replies_are_logged_but_only_original_messages_are_queued(self):
        jobs = queue.SimpleQueue()
        self.listener.worker = SimpleNamespace(submit=jobs.put)
        for index, fields in enumerate((
                {"parent_id": "om_parent"},
                {"root_id": "om_root"},
                {"parent_id": "om_parent", "root_id": "om_root", "thread_id": "omt_thread"})):
            event = copy.deepcopy(self.event)
            event["event"]["message"].update(message_id=f"om_reply_{index}", **fields)
            self.listener.handle(event)
        original = copy.deepcopy(self.event)
        original["event"]["message"].update(
            message_id="om_original", root_id="om_original", thread_id="omt_thread", chat_type="topic_group")
        self.listener.handle(original)
        self.assertEqual(jobs.qsize(), 1)
        self.assertEqual(jobs.get_nowait()["message_id"], "om_original")
        self.assertEqual(self.log_path.read_text().count("论文分享"), 4)

    def test_main_runs_live_event_through_discovery_and_summary_worker(self):
        import lark_oapi as lark
        from paper_analyzer import PaperAnalyzer

        url = "https://example.org/paper"
        self.event["event"]["message"]["content"] = json.dumps({"text": url})
        summary = "本摘要由 gpt-6-astra 生成\n论文\n• 问题：适应新任务。\n• 创新：复用经验。\n• 方法：探索后更新程序。\n• 结果：42 对比 30。\n" + url
        summary += "\n• 局限性（AI 分析）：真机泛化尚未验证。"
        summary += "。" * (1000 - len(summary))
        replies = [
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "read", "type": "function", "function": {
                    "name": "read_source", "arguments": json.dumps({"url": url})}}]},
            {"role": "assistant", "content": json.dumps({
                "status": "verified", "reason": "Title and method match.", "urls": [url]})},
            {"role": "assistant", "content": summary},
        ]
        responses = [io.BytesIO(json.dumps({"choices": [{
            "message": reply, "finish_reason": "tool_calls" if reply.get("tool_calls") else "stop",
        }]}).encode()) for reply in replies]
        analyzer = PaperAnalyzer(api_key="dummy", output_dir=self.log_path.parent / "analyses",
                                 papers_dir=self.log_path.parent / "papers")
        builder = Mock()
        builder.register_p2_im_message_receive_v1.return_value = builder
        builder.register_p2_im_message_reaction_created_v1.return_value = builder
        builder.register_p2_im_message_reaction_deleted_v1.return_value = builder

        def deliver():
            with self.assertRaisesRegex(RuntimeError, "already running"):
                monitor.main()
            callback = builder.register_p2_im_message_receive_v1.call_args.args[0]
            callback({"event": "malformed-private-payload"})
            callback(self.event)
            callback(self.event)
            for register in (builder.register_p2_im_message_reaction_created_v1,
                             builder.register_p2_im_message_reaction_deleted_v1):
                self.assertIsNone(register.call_args.args[0](object()))

        console = io.StringIO()
        with patch.dict(os.environ, {"PAPER_APP_ID": "cli_test", "PAPER_APP_SECRET": "dummy"}, clear=True), \
                patch("monitor.__file__", str(self.log_path.parent / "monitor.py")), \
                patch("sys.argv", ["monitor.py", "--chat-id", "oc_paper", "--output", str(self.log_path)]), \
                patch("monitor.load_history", return_value=0), \
                patch.object(lark.EventDispatcherHandler, "builder", return_value=builder), \
                patch.object(lark.ws, "Client") as websocket, \
                patch("paper_analyzer.PaperAnalyzer.from_env", return_value=analyzer), \
                patch("paper_discovery.fetch_source", return_value={
                    "url": url, "retrieved_url": url, "text": "Robot Learning. Score 42 versus 30.",
                    "truncated": False}), \
                patch("paper_analyzer.archive_papers", return_value=([], [])), \
                patch("lark_oapi.api.im.v1.resource.message.Message.reply", return_value=SimpleNamespace(
                    success=lambda: True, code=0)) as send, \
                patch("paper_analyzer.urllib.request.OpenerDirector.open", side_effect=responses), \
                redirect_stdout(self.output), redirect_stderr(console):
            websocket.return_value.start.side_effect = deliver
            monitor.main()
        self.assertEqual(self.log_path.read_text(), url + "\n")
        self.assertEqual(self.output.getvalue(), url + "\n")
        self.assertIn(summary, console.getvalue())
        self.assertIn("Message handling failed (AttributeError)", console.getvalue())
        self.assertNotIn("malformed-private-payload", console.getvalue())
        reports = list(analyzer.output_dir.glob("*.md"))
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].read_text(), summary + "\n")
        self.assertEqual(send.call_count, 1)
        request = send.call_args.args[0]
        self.assertEqual(request.paths["message_id"], "om_1")
        self.assertTrue(request.body.reply_in_thread)
        self.assertEqual(request.body.msg_type, "post")
        post = json.loads(request.body.content)["zh_cn"]
        self.assertEqual(post["title"], "📄 论文")
        displayed = "".join(node.get("text", "") for row in post["content"] for node in row)
        self.assertIn("📊 结果：42 对比 30。", displayed)

    def test_loads_latest_three_history_messages_oldest_first_and_deduplicates_live(self):
        items = [SimpleNamespace(message_id=f"om_{index}", chat_id="oc_paper",
                                 create_time="0", sender=SimpleNamespace(id="ou_user", sender_name=None),
                                 msg_type="text", body=SimpleNamespace(content=json.dumps({"text": str(index)})))
                 for index in (3, 2, 1)]
        client = Mock()
        client.im.v1.message.list.return_value = SimpleNamespace(
            success=lambda: True, data=SimpleNamespace(items=items))
        self.assertEqual(monitor.load_history(client, self.listener), 3)
        request = client.im.v1.message.list.call_args.args[0]
        queries = dict(request.queries)
        self.assertEqual(queries["container_id"], "oc_paper")
        self.assertEqual(queries["container_id_type"], "chat")
        self.assertEqual(queries["page_size"], "3")
        self.assertEqual(queries["sort_type"], "ByCreateTimeDesc")
        expected = "".join(f"[08-00:00] ou_user: {index}\n" for index in (1, 2, 3))
        self.assertEqual(self.output.getvalue(), expected)
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), expected)
        self.assertIsNone(self.listener.handle(self.event))

    def test_history_permission_failure_is_explicit_and_does_not_log_messages(self):
        client = Mock()
        client.im.v1.message.list.return_value = SimpleNamespace(success=lambda: False, code=99991672)
        with self.assertRaisesRegex(RuntimeError, "99991672.*im:message.history:readonly"):
            monitor.load_history(client, self.listener)
        self.assertEqual(self.output.getvalue(), "")
        self.assertFalse(self.log_path.exists())

    def test_empty_history_does_not_create_log(self):
        client = Mock()
        client.im.v1.message.list.return_value = SimpleNamespace(
            success=lambda: True, data=SimpleNamespace(items=None))
        self.assertEqual(monitor.load_history(client, self.listener), 0)
        self.assertFalse(self.log_path.exists())

    def test_history_prints_only_timestamp_user_and_post_text_even_in_debug(self):
        self.listener.debug = io.StringIO()
        post = {"title": "论文", "content": [[{"tag": "text", "text": "test"},
                {"tag": "a", "text": " paper", "href": "https://example.org"}]],
                "content_v2": [[{"tag": "text", "text": "must not repeat"}]]}
        item = SimpleNamespace(message_id="om_post", chat_id="oc_paper", create_time="1792976274439",
                               sender=SimpleNamespace(id="ou_user", sender_name="Jiahe"),
                               msg_type="post", body=SimpleNamespace(content=json.dumps(post)))
        client = Mock()
        client.im.v1.message.list.return_value = SimpleNamespace(
            success=lambda: True, data=SimpleNamespace(items=[item]))
        monitor.load_history(client, self.listener)
        expected = "[08-57:54] Jiahe: 论文\ntest paper\n"
        self.assertEqual(self.output.getvalue(), expected)
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), expected)
        self.assertEqual(self.listener.debug.getvalue(), "")

    def test_debug_ignores_other_chats(self):
        debug = io.StringIO()
        self.listener.debug = debug
        self.event["event"]["message"]["chat_id"] = "oc_other"
        self.listener.handle(self.event)
        self.assertEqual(debug.getvalue(), "")
        self.assertEqual(self.output.getvalue(), "")
        self.assertFalse(self.log_path.exists())

    def test_debug_live_post_shows_timestamp_sender_and_text_once(self):
        self.listener.debug = io.StringIO()
        self.event["event"]["message"].update(
            message_type="post", create_time="1792976551631",
            content=json.dumps({"title": "", "content": [[{"tag": "text", "text": "test"}]],
                                "content_v2": [[{"tag": "text", "text": "test"}]]}))
        result = self.listener.handle(self.event)
        self.assertEqual(result, self.event["event"]["message"])
        expected = "[09-02:31] ou_dummy: test\n"
        self.assertEqual(self.output.getvalue(), expected)
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), expected)
        self.assertEqual(self.listener.debug.getvalue(), "")
        self.assertIsNone(self.listener.handle(self.event))
        self.assertEqual(self.output.getvalue(), expected)

    def test_topic_group_messages_are_printed_saved_and_returned_for_analysis(self):
        self.event["event"]["message"]["chat_type"] = "topic_group"
        self.assertEqual(self.listener.handle(self.event), self.event["event"]["message"])
        self.assertEqual(self.output.getvalue(), "论文分享\n")
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), "论文分享\n")

    def test_other_topic_groups_are_ignored(self):
        self.event["event"]["message"].update(chat_type="topic_group", chat_id="oc_other")
        self.assertIsNone(self.listener.handle(self.event))
        self.assertEqual(self.output.getvalue(), "")
        self.assertFalse(self.log_path.exists())

    def test_appends_to_existing_file_and_preserves_multiline_text(self):
        self.log_path.write_text("previous message\n", encoding="utf-8")
        self.event["event"]["message"]["content"] = json.dumps({"text": "论文\nhttps://example.org/paper"})
        self.listener.handle(self.event)
        self.assertEqual(self.log_path.read_text(encoding="utf-8"),
                         "previous message\n论文\nhttps://example.org/paper\n")

    def test_nontext_and_invalid_json_are_saved_verbatim(self):
        for index, (kind, content) in enumerate((
            ("image", '{"image_key":"dummy"}'),
            ("text", "raw invalid JSON"),
        )):
            event = copy.deepcopy(self.event)
            event["event"]["message"].update(
                message_type=kind, content=content, message_id=f"om_raw_{index}")
            self.listener.handle(event)
        expected = '{"image_key":"dummy"}\nraw invalid JSON\n'
        self.assertEqual(self.output.getvalue(), expected)
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), expected)

    def test_failed_file_write_does_not_print_or_mark_as_seen(self):
        jobs = queue.SimpleQueue()
        self.listener.worker = SimpleNamespace(submit=jobs.put)
        self.listener.log_path = self.log_path.parent / "missing" / "messages.txt"
        with self.assertRaises(OSError):
            self.listener.handle(self.event)
        self.assertEqual(self.output.getvalue(), "")
        self.assertTrue(jobs.empty())
        self.listener.log_path = self.log_path
        self.listener.handle(self.event)
        self.assertEqual(self.output.getvalue(), "论文分享\n")
        self.assertEqual(jobs.get_nowait(), self.event["event"]["message"])

    def test_other_groups_and_unknown_chat_types_are_ignored(self):
        for changes in ({"chat_id": "oc_other"}, {"chat_type": "unknown"}):
            event = copy.deepcopy(self.event)
            event["event"]["message"].update(changes)
            self.listener.handle(event)
        self.assertEqual(self.output.getvalue(), "")
        self.assertFalse(self.log_path.exists())

    def test_private_messages_from_different_chats_are_queued_once(self):
        jobs = queue.SimpleQueue()
        self.listener.worker = SimpleNamespace(submit=jobs.put)
        for index in (1, 2):
            event = copy.deepcopy(self.event)
            event["event"]["message"].update(
                chat_type="p2p", chat_id=f"oc_private_{index}", message_id=f"om_private_{index}")
            self.listener.handle(event)
            self.listener.handle(event)
        self.assertEqual(jobs.qsize(), 2)
        self.assertEqual([jobs.get_nowait()["chat_id"] for _ in range(2)],
                         ["oc_private_1", "oc_private_2"])
        self.assertEqual(self.log_path.read_text(), "论文分享\n论文分享\n")

    def test_private_bot_messages_replies_and_history_do_not_trigger_analysis(self):
        jobs = queue.SimpleQueue()
        self.listener.worker = SimpleNamespace(submit=jobs.put)
        for index, fields in enumerate(({}, {"parent_id": "om_parent"}, {"root_id": "om_root"}, {})):
            event = copy.deepcopy(self.event)
            event["event"]["message"].update(
                chat_type="p2p", chat_id="oc_private", message_id=f"om_private_{index}", **fields)
            if index == 0:
                event["event"]["sender"]["sender_type"] = "app"
            self.listener.handle(event, history=index == 3)
        self.assertTrue(jobs.empty())
        self.assertEqual(len(self.log_path.read_text().splitlines()), 4)

    def test_private_message_without_chat_id_is_ignored(self):
        self.event["event"]["message"].update(chat_type="p2p", chat_id="")
        self.assertIsNone(self.listener.handle(self.event))
        self.assertFalse(self.log_path.exists())

    def test_all_delivered_message_types_are_preserved(self):
        for index, kind in enumerate(("text", "post", "image", "file", "audio", "media", "sticker", "interactive")):
            event = copy.deepcopy(self.event)
            event["event"]["message"].update(message_type=kind, message_id=f"om_{index}")
            self.listener.handle(event)
        self.assertEqual(len(self.output.getvalue().splitlines()), 8)

    def test_redelivery_is_ignored(self):
        self.assertEqual(self.listener.handle(self.event), self.event["event"]["message"])
        self.assertIsNone(self.listener.handle(self.event))
        self.assertEqual(len(self.output.getvalue().splitlines()), 1)
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), "论文分享\n")

    def test_incomplete_events_are_ignored(self):
        for event in ({}, {"event": {}}, {"event": {"message": {"chat_id": "oc_paper"}}}):
            self.listener.handle(event)
        self.assertEqual(self.output.getvalue(), "")

    def test_failed_console_write_does_not_duplicate_saved_message(self):
        class BrokenOutput:
            def write(self, value):
                raise OSError("disk full")

        self.listener.output = BrokenOutput()
        with self.assertRaises(OSError):
            self.listener.handle(self.event)
        self.listener.output = self.output
        self.listener.handle(self.event)
        self.assertEqual(self.output.getvalue(), "")
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), "论文分享\n")


if __name__ == "__main__":
    unittest.main()
