"""Monitor a Feishu group and private chats for paper summaries. Credentials are read only by main()."""

import argparse
from collections import OrderedDict
from datetime import datetime
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
from threading import Lock
from zoneinfo import ZoneInfo


def credentials(env):
    # Keep the requested names; app credentials are required for receiving.
    if env.get("PAPER_APP_ID") or env.get("PAPER_APP_SECRET"):
        app_id = env.get("PAPER_APP_ID", "")
        app_secret = env.get("PAPER_APP_SECRET", "")
    else:
        app_id = env.get("PAPER_BOT_URL", "")
        app_secret = env.get("PAPER_BOT_KEY", "")
    if app_id.startswith(("https://", "http://")):
        raise ValueError(
            "Webhook credentials cannot receive group messages. Keep PAPER_BOT_URL "
            "and PAPER_BOT_KEY unchanged and add PAPER_APP_ID and PAPER_APP_SECRET "
            "from a Feishu custom app with bot capability."
        )
    if not app_id.startswith("cli_") or not app_secret:
        raise ValueError(
            "App credentials required: set PAPER_APP_ID and PAPER_APP_SECRET, "
            "or use PAPER_BOT_URL for the app ID (cli_...) and PAPER_BOT_KEY "
            "for the app secret."
        )
    return app_id, app_secret


def chat_list_error(code):
    if code == 232034:
        hint = (
            "The app is not installed or enabled in this tenant. "
            "Open 版本管理与发布, publish the app and complete approval; "
            "have a tenant administrator install/enable it if needed."
        )
    else:
        hint = "Check app installation, activation and permissions."
    return f"Cannot list groups (Feishu code {code}). {hint}"


def find_chat(name, fetch_page):
    matches = set()
    token = None
    visited = set()
    while True:
        items, next_token = fetch_page(token)
        matches.update(item["chat_id"] for item in items if item.get("name") == name)
        if not next_token:
            break
        if next_token in visited:
            raise RuntimeError("Feishu returned a repeated group-list page token.")
        visited.add(next_token)
        token = next_token
    if not matches:
        raise ValueError(f"Group {name!r} not found. Add the app bot to that group first.")
    if len(matches) > 1:
        raise ValueError(f"Multiple groups named {name!r}; select one with --chat-id.")
    return matches.pop()


def message_line(message, text, sender):
    if message.get("message_type") == "post":
        try:
            post = json.loads(text)
            if isinstance(post, dict) and "content" not in post and "content_v2" not in post:
                post = next(iter(post.values()), {})
            if isinstance(post, dict):
                rows = post.get("content") or post.get("content_v2") or []
                lines = [post.get("title") or ""]
                for row in rows:
                    lines.append("".join(node.get("text") or node.get("user_name") or ""
                                         for node in row if isinstance(node, dict)))
                extracted = "\n".join(line for line in lines if line)
                if extracted:
                    text = extracted
        except (json.JSONDecodeError, TypeError):
            pass
    timestamp = message.get("create_time") or "unknown time"
    try:
        timestamp = datetime.fromtimestamp(int(timestamp) / 1000, ZoneInfo("Asia/Shanghai")).strftime("%H-%M:%S")
    except (ValueError, OverflowError, OSError):
        pass
    return f"[{timestamp}] {sender or 'unknown user'}: {text}"


def summary_post(summary):
    from paper_analyzer import SUMMARY_HEADER_RE

    disclosure = SUMMARY_HEADER_RE.match(summary).group().strip()
    lines = SUMMARY_HEADER_RE.sub("", summary, count=1).strip().splitlines()
    sections = {"问题": "🎯", "创新": "💡", "方法": "🛠️", "结果": "📊",
                "局限性（AI 分析）": "⚠️", "备注": "📝"}
    title = "📄 论文速读"
    if lines and not re.match(r"(?:[•\-*]\s*)?(?:问题|创新|方法|结果|局限性（AI 分析）|备注)：|https?://", lines[0]):
        title = "📄 " + lines.pop(0)
    rows = [[{"tag": "text", "text": "🤖 " + disclosure, "style": ["italic"]}]]
    for line in lines:
        line = line.strip()
        if not line:
            continue
        section = re.fullmatch(r"(?:[•\-*]\s*)?(问题|创新|方法|结果|局限性（AI 分析）|备注)：\s*(.*)", line)
        if section:
            label, text = section.groups()
            rows.append([{"tag": "text", "text": "\n"}])
            rows.append([{"tag": "text", "text": f"{sections[label]} {label}：", "style": ["bold"]}])
            if text:
                rows.append([{"tag": "text", "text": text}])
        elif re.fullmatch(r"https?://\S+", line):
            rows.append([{"tag": "a", "text": "🔗 阅读原文", "href": line}])
        else:
            rows.append([{"tag": "text", "text": line}])
    return {"zh_cn": {"title": title, "content": rows}}


def reply_summary(client, message, summary):
    from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
    from requests import RequestException
    from paper_analyzer import MAX_SUMMARY_CHARS, SUMMARY_HEADER, SUMMARY_HEADER_RE

    message_id = message.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("A reply requires the original message ID.")
    if not summary or summary.startswith("Analysis unavailable:"):
        raise ValueError("Only a successful paper summary can be sent.")
    if not SUMMARY_HEADER_RE.match(summary):
        summary = SUMMARY_HEADER + "\n" + summary
    if (len(summary) > MAX_SUMMARY_CHARS
            or any(label not in summary for label in ("问题：", "创新：", "方法：", "结果：", "局限性（AI 分析）："))):
        raise ValueError("Only a complete Chinese paper summary within the character limit can be sent.")
    body = (ReplyMessageRequestBody.builder().msg_type("post")
            .content(json.dumps(summary_post(summary), ensure_ascii=False))
            .reply_in_thread(message.get("chat_type") != "p2p")
            .uuid("paper-summary-" + hashlib.sha256(message_id.encode()).hexdigest()[:32]).build())
    request = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
    try:
        response = client.im.v1.message.reply(request)
    except (RequestException, OSError):
        raise RuntimeError("Feishu reply connection failed; the report is saved locally.") from None
    if not response.success():
        if response.code in (230011, 230019):
            raise RuntimeError(f"Feishu reply failed (code {response.code}): the original message was recalled "
                               "or its topic no longer exists; the report is saved locally.")
        raise RuntimeError(f"Feishu reply failed (code {response.code}); check im:message:send_as_bot, "
                           "app publication and chat access.")


class MessageMonitor:
    def __init__(self, chat_id, output, log_path="messages.txt", debug=None, worker=None):
        self.chat_id = chat_id
        self.output = output
        self.log_path = log_path
        self.debug = debug
        self.worker = worker
        self.seen = OrderedDict()
        self.lock = Lock()

    def handle(self, payload, history=False):
        event = payload.get("event") or {}
        message = event.get("message") or {}
        message_id = message.get("message_id")
        chat_type = message.get("chat_type")
        if (not message_id or not message.get("chat_id")
                or chat_type not in ("group", "topic_group", "p2p")
                or (chat_type != "p2p" and message.get("chat_id") != self.chat_id)):
            return
        with self.lock:
            if message_id in self.seen:
                return
            text = message.get("content") or ""
            if message.get("message_type") == "text":
                try:
                    content = json.loads(text)
                except json.JSONDecodeError:
                    content = None
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    text = content["text"]
            if history or self.debug is not None:
                sender_id = ((event.get("sender") or {}).get("sender_id") or {})
                sender = message.get("sender") if history else sender_id.get("open_id")
                text = message_line(message, text, sender)
            descriptor = os.open(self.log_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as saved:
                print(text, file=saved, flush=True)
            self.seen[message_id] = None
            # shortcut: deduplication covers the last 10,000 messages in this process;
            # use persistent storage if deduplication across restarts is needed.
            if len(self.seen) > 10_000:
                self.seen.popitem(last=False)
            print(text, file=self.output, flush=True)
        sender = (payload.get("event") or {}).get("sender") or {}
        if (not history and self.worker is not None and sender.get("sender_type") != "app"
                and not message.get("parent_id")
                and message.get("root_id") in (None, "", message_id)):
            self.worker.submit(message)
        return message


def load_history(client, listener):
    from lark_oapi.api.im.v1 import ListMessageRequest

    request = (ListMessageRequest.builder().container_id_type("chat")
               .container_id(listener.chat_id).sort_type("ByCreateTimeDesc")
               .page_size(3).build())
    response = client.im.v1.message.list(request)
    if not response.success():
        raise RuntimeError(
            f"Cannot load history (Feishu code {response.code}). Check "
            "im:message.history:readonly and im:message.group_msg permissions; "
            "publish and approve the updated app version."
        )
    if response.data is None:
        raise RuntimeError("Feishu returned no history data.")
    loaded = 0
    for item in reversed(response.data.items or []):
        message = {"chat_id": item.chat_id, "chat_type": "group",
                   "message_id": item.message_id, "message_type": item.msg_type,
                   "create_time": item.create_time,
                   "sender": (getattr(item.sender, "sender_name", None) or item.sender.id)
                   if item.sender else None,
                   "content": item.body.content if item.body else ""}
        if listener.handle({"event": {"message": message}}, history=True) is not None:
            loaded += 1
    return loaded


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", default="paper分享", help="Exact group name (default: paper分享)")
    parser.add_argument("--chat-id", help="Explicit chat ID; skips group-name lookup")
    parser.add_argument("--output", default="messages.txt", help="Append raw text to this file (default: messages.txt)")
    parser.add_argument("--no-analysis", action="store_true", help="Only log messages; do not analyze links")
    parser.add_argument("--debug", action="store_true", help="Log message timestamp, sender and text")
    parser.add_argument("--replay", metavar="JSON_FILE", help="Summarize saved messages locally, without connecting to Feishu")
    args = parser.parse_args()
    if args.replay:
        if args.no_analysis:
            parser.error("--replay requires analysis; remove --no-analysis")
        messages = json.loads(Path(args.replay).read_text(encoding="utf-8"))
        if not isinstance(messages, list) or any(
                not isinstance(message, dict) or not isinstance(message.get("content"), str)
                or not message["content"].strip() for message in messages):
            raise ValueError("Replay input must be a JSON list of messages with nonempty string content.")
        from paper_analyzer import PaperAnalyzer
        analyzer = PaperAnalyzer.from_env(os.environ)
        for index, message in enumerate(messages, 1):
            print(f"Summarizing replay message {index}/{len(messages)}...", flush=True)
            path = analyzer.analyze(message)
            if path is None:
                print("Skipped: message does not identify a paper/report.", flush=True)
                continue
            print(path.read_text(encoding="utf-8"), flush=True)
            print(f"Analysis report saved: {path}", flush=True)
        return
    with Path(__file__).with_name(".monitor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("The Feishu monitor is already running in another process.") from None
        run_live(args)


def run_live(args):
    app_id, app_secret = credentials(os.environ)

    import lark_oapi as lark
    from lark_oapi.api.im.v1 import ListChatRequest
    from requests import RequestException

    formatter = logging.Formatter("[Lark] [%(asctime)s] [%(levelname)s] %(message)s", datefmt="%H-%M:%S")
    formatter.converter = lambda seconds: datetime.fromtimestamp(seconds, ZoneInfo("Asia/Shanghai")).timetuple()
    for handler in logging.getLogger("Lark").handlers:
        handler.setFormatter(formatter)

    client = (lark.Client.builder().app_id(app_id).app_secret(app_secret)
              .timeout(15).log_level(lark.LogLevel.ERROR).build())
    chat_id = args.chat_id
    if not chat_id:
        def fetch_page(token):
            builder = ListChatRequest.builder().page_size(100)
            if token:
                builder.page_token(token)
            response = client.im.v1.chat.list(builder.build())
            if not response.success():
                raise RuntimeError(chat_list_error(response.code))
            if response.data is None:
                raise RuntimeError("Feishu returned no group-list data.")
            data = response.data
            if data.has_more and not data.page_token:
                raise RuntimeError("Feishu returned an incomplete group-list page.")
            return ([{"name": item.name, "chat_id": item.chat_id}
                     for item in (data.items or [])],
                    data.page_token if data.has_more else None)

        chat_id = find_chat(args.group, fetch_page)

    from paper_analyzer import MAX_SUMMARY_CHARS, AnalysisWorker, PaperAnalyzer

    worker = None if args.no_analysis else AnalysisWorker(
        PaperAnalyzer.from_env(os.environ), reply=lambda message, summary: reply_summary(client, message, summary))
    listener = MessageMonitor(chat_id, sys.stdout, args.output,
                              debug=sys.stderr if args.debug else None, worker=worker)
    if worker is not None:
        print(f"Paper summaries enabled: source discovery → Chinese summary, "
              f"≤{MAX_SUMMARY_CHARS} characters; reports: {worker.analyzer.output_dir}",
              file=sys.stderr, flush=True)
    print("Loading the latest 3 history messages...", file=sys.stderr, flush=True)
    try:
        loaded = load_history(client, listener)
        print(f"Loaded {loaded} history messages.", file=sys.stderr, flush=True)
    except RuntimeError as error:
        print(f"{error} Continuing with live messages.", file=sys.stderr, flush=True)
    except RequestException as error:
        print(f"History request failed ({type(error).__name__}); continuing with live messages.",
              file=sys.stderr, flush=True)
    def on_message(data):
        try:
            listener.handle(json.loads(lark.JSON.marshal(data)))
        except Exception as error:
            print(f"Message handling failed ({type(error).__name__}); continuing to listen.",
                  file=sys.stderr, flush=True)

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message)
               .register_p2_im_message_reaction_created_v1(lambda data: None)
               .register_p2_im_message_reaction_deleted_v1(lambda data: None).build())
    print(f"Listening for group messages in {chat_id} and private messages to the bot; "
          f"summary replies {'disabled' if worker is None else 'enabled'}.", file=sys.stderr)
    try:
        lark.ws.Client(app_id, app_secret, event_handler=handler,
                       log_level=lark.LogLevel.ERROR).start()
    finally:
        if worker is not None:
            print("Finishing queued paper analyses; Ctrl+C again to exit immediately.", file=sys.stderr)
            worker.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except (ValueError, RuntimeError, ImportError) as error:
        print(f"Monitor could not start: {error}", file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print(f"Monitor stopped unexpectedly ({type(error).__name__}); restart required.", file=sys.stderr)
        sys.exit(1)
