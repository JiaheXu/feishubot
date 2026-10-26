# Feishu paper group monitor

A Python listener and paper-summary bot for **paper分享**. It prints each message's text
to the terminal and appends the same text to **messages.txt** in the current
working directory. Successful summaries are sent as threaded replies to the
original message, and are also printed and saved locally.

Research messages are analyzed in a background worker using **gpt-6-astra**
through your local OpenAI-compatible proxy. Research messages use two AI stages:
source discovery/verification, then summarization of the confirmed links and
retrieved text. Each stage may use multiple model/tool calls. Reports are
printed locally and saved under **analyses/**; raw logging continues while
analysis runs. Messages with no identifiable paper/report are skipped after
classification, without a summary file or group reply. Failed analyses stay local.

## Credentials

Credentials are read from your process environment only when you launch the
listener. Importing the module, running tests, and `--help` do not read them.
The program does not automatically open `.env` files or print credentials.

The requested names `PAPER_BOT_URL` and `PAPER_BOT_KEY` are supported **if their
values are an app ID (`cli_...`) and app secret**, respectively. If they contain
a group webhook URL and signing key, keep them unchanged and also configure
`PAPER_APP_ID` and `PAPER_APP_SECRET`. Those app variables take precedence.
Webhook credentials cannot receive messages, and the program reports this
instead of pretending to monitor the group.

## Feishu setup

1. Create an enterprise custom app (自建应用) at <https://open.feishu.cn/app>
   and enable its bot capability. A group's custom webhook bot is a different
   bot type and cannot read messages.
2. Grant **获取群组中所有消息** (`im:message.group_msg`) so receiving does not
   require an @mention. For automatic group lookup, also grant
   `im:chat:read` (查看群信息); `im:chat:readonly` also works. Lookup can be
   skipped with `--chat-id`.
   For summary replies, also grant **以应用的身份发消息**
   (`im:message:send_as_bot`), then publish and approve the updated app version.
3. Configure event delivery to **使用长连接接收事件** and subscribe to
   **接收消息 v2.0** (`im.message.receive_v1`). No public HTTP server is needed.
   Feishu may require the listener to be connected before saving this setting;
   use `--chat-id` to establish the connection before group lookup is ready.
4. Publish the app version, obtain any required administrator approval, and
   add the app bot to **paper分享**.
5. Set app credentials in your local runtime environment. See `.env.example`
   for variable names; do not commit real values.

## Run locally

Requires Python 3.10+ and outbound access to Feishu.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# Configure/export your environment variables locally before running:
./launch.sh
```

`launch.sh` follows the proxy configuration in `download_paper.sh`, loads and
exports `local_env.sh` if present, changes to this project directory, and runs
`monitor.py`. It uses `.venv/bin/python` when available, otherwise `python3`.
Set `PYTHON_BIN` to choose a different interpreter. Arguments are forwarded:

```bash
./launch.sh --chat-id oc_your_group_id
./launch.sh --no-analysis  # Raw logging only
./launch.sh --no-analysis --debug  # Message timestamp, sender and text
./launch.sh --replay examples/history_messages.json  # Saved group messages; no Feishu connection
```

## Run in the background

The `feishubot.service` user service starts at login and restarts the bot after
process exits, waiting 10 seconds between attempts. With user lingering enabled,
it also starts at boot and continues after logout. It uses `launch.sh` and its
private, ignored `local_env.sh` configuration. On this host:

```bash
systemctl --user link /home/jiahe/feishubot/feishubot.service
systemctl --user daemon-reload
systemctl --user enable --now feishubot.service
systemctl --user status feishubot.service
journalctl --user -u feishubot.service -n 50 --no-pager
systemctl --user restart feishubot.service  # Apply Python changes
systemctl --user stop feishubot.service
```

Live instances use a process lock to prevent duplicate listeners. The SDK
reconnects after network interruptions; event-handler errors are logged by type
without printing the event payload, and analysis failures do not stop later jobs.
Stopping the service lets queued analyses finish for up to 35 minutes before
systemd forces shutdown. Pending jobs are held in memory and can be lost on a
crash or forced shutdown; failed analyses/replies keep their local reports.
Credentials, network access and the local AI proxy must remain available.

Paper summaries are enabled by default. The startup line confirms the character
limit and report directory. Each accepted live message is logged and queued
once by the monitor; duplicates, other chats, replies and startup history are not queued.
Replies are identified by `parent_id` or a `root_id` pointing to another message;
a `thread_id` alone does not exclude an original topic post. Replies remain in the raw log.
The background worker finds and verifies sources, then generates the Chinese
**问题 / 创新 / 方法 / 结果 / 局限性（AI 分析）** summary within **1,000 characters** and replies
under the original message as a rich-text post: a 📄 paper title, emoji and bold
section headings, spaced paragraphs, and clickable source links. Local reports
keep their plain-text format. Every saved summary starts with `本摘要由 <模型名称> 生成`,
using the proxy response's model ID, or the configured `AI_MODEL` / `OPENAI_MODEL`
when the response omits it;
this line counts towards the 1,000-character limit. Each original message has a stable reply UUID for
server-side deduplication. App/bot messages remain logged but do not trigger
analysis, preventing reply loops. Reaction-created/deleted events are ignored.
Use `--no-analysis` to disable analysis and replies while keeping raw logging.

With analysis enabled (`./launch.sh --debug`), completed summaries are printed
locally as well as saved under `analyses/`. The replay set contains 13 messages:
the original title-only **Scaling Online Exploration for Robot Agents** and
linked **Giving Robots a Sense of Touch** examples, plus 10 messages transcribed
from group-history screenshots (Motubrain, real2sim2real, ManiMux, arXiv
2610.28480, linked RAX, Dream-1, FLORA, Robota-Embed, Microsoft pre-training,
and T2-Z), plus an ASPIRE PDF rich-post regression case with the caption
`test msg`. Message IDs are fixture labels; sender names and reactions are omitted.
The screenshots' long signed Facebook CDN URL and clipped bottom message are
not transcribed because their full content cannot be read reliably.
It makes real public-source and AI requests; it does not read Feishu history,
start a listener, or post messages. Replay files are JSON lists of objects with
`message_id` and string `content` fields. The latest three messages fetched at
normal listener startup remain logging-only.

With `--debug`, each new selected-chat message is printed and saved once as
`[HH-MM:SS] sender: text` (Asia/Shanghai). SDK payloads, connection URLs, heartbeats, and event
metadata are suppressed; SDK errors remain visible. For live messages the sender
is the user's Feishu open ID. Startup status lines remain visible. If new events
do not arrive, check the event subscription and run only one listener instance.
History output stays concise even in debug mode: timestamp (Asia/Shanghai),
sender name when returned by Feishu (otherwise sender ID), and message text.
Topic posts are rendered as their title and text paragraphs without JSON wrappers.

The reference `download_paper.sh` is left unchanged. Its `download_paper.py`
target was not included here; this monitor implements its own source retrieval.

## AI configuration and editable prompt

The defaults match your reference script:

| Setting | Default |
| --- | --- |
| Chat Completions endpoint | `http://127.0.0.1:8787/v1/chat/completions` |
| Model | `gpt-6-astra` |
| Proxy API key | `codex-proxy` (placeholder, as in the reference) |
| Summary prompt | `paper_prompt.txt` beside the Python files |
| Discovery prompt | `discovery_prompt.txt` beside the Python files |
| Summary directory | `analyses/` in the current working directory |
| PDF directory | `papers/` in the current working directory |

The summary is one detailed Chinese reading-group message: original title,
**问题**, **创新**, **方法**, **结果**, one source URL, and a final
**⚠️ 局限性（AI 分析）** section. Limitations are explicitly attributed to AI,
grounded in the available sources, and distinguish reported limitations from
AI inferences. There is no detailed report or printed discovery/coverage header.
The limit is **1,000 characters**, including title, URL, newlines and limitations,
with no whitespace word limit; an oversized draft is shortened
once, then reported as a failure if it still exceeds the limit.
**Edit `paper_prompt.txt` to change instructions, language, or format.** It is
reloaded for each request; the body aims for 650–800 Chinese characters when
the sources support that detail. Methods include training/inference flow and
available model/data settings; results prioritize 2–3 quantitative comparisons with
named baselines, units, evaluation conditions and sample counts where available.
Sparse sources can produce shorter summaries without invented numbers. Discovery and retrieval
diagnostics are saved beside each `.md` summary in a matching `.json` file.

The summary follows `~/Embodiment_structure/summarize_paper.py`: ground the
problem and contribution in the introduction, check them against methods and
experiments, and separate measured results from author claims.

Run the saved message examples with a separate script (real source/AI requests,
no Feishu connection or sending). It uses the exported AI/proxy environment
settings above and saves reports under `analyses/message_tests/`:

```bash
.venv/bin/python summary_messages.py
.venv/bin/python summary_messages.py examples/history_messages.json
.venv/bin/python summary_messages.py examples/rax_message.json  # Direct link; skips title search
.venv/bin/python summary_messages.py examples/project_homepage_message.json  # Homepage href → paper
python3 -m unittest test_summary_messages -v  # Offline checks of the runner
```

Use `.venv/bin/python` for live tests so the installed `pypdf` dependency is
available. A system `python3` without `pypdf` cannot extract PDF text and may
fall back to an arXiv abstract page.

The script prints each summary and PASS/FAIL, or SKIP for nonresearch messages,
continues after failed messages,
and exits nonzero if a summary fails, lacks any of the four Chinese labels,
or exceeds 1,000 characters. The runner shares the analyzer's character limit.

`discovery_prompt.txt` controls the first AI stage for all messages, including
those with URLs. The program reads supplied URLs before the first AI decision
and shares the fetched text or errors with the classifier. Captions such as
`test msg` do not override a linked paper; the document contents determine its
type. Repeated rich-post URLs are read once during discovery. Title-only messages require search.
Project pages are followed to their main paper/report links before classification,
including citation PDF metadata and common Paper/PDF/arXiv buttons. Relative URLs,
icon-decorated labels and accessible button labels are supported. This traversal
shares the existing 10-link / 300,000-character budget, avoids re-reading arXiv's
PDF after its paper was already retrieved, and does not follow arbitrary references.
Broken paper links remain visible to the AI; a substantive project report may
still be summarized with a caveat rather than inventing a missing paper.
The AI can search arXiv metadata, search the public web, and read
public sources, with at most 8 tool calls and 300,000 retrieved characters per
message. It must read a source before selecting it. Ambiguous matches, ordinary
chatter, blocked searches, and missing sources never produce invented summaries.
Ordinary chatter returns SKIP; ambiguous/unavailable sources leave local failure
reports without posting to the group. The second AI stage receives explicit `paper_urls`
and the already retrieved source text, then produces the Chinese summary without
having to download the same paper again. If discovery fails or no source is
readable, the summary stage is skipped. Both prompts reload for each job. The configured
AI proxy must support Chat Completions function/tool calls for discovery.
Public web search uses DuckDuckGo's HTML results and can be blocked by a
challenge. Paper search tries arXiv, then Hugging Face's paper index if the
arXiv request fails, including HTTP 429. Candidates still require source reading
and verification before summarization. This first version
does not crawl citations from linked blogs or persist paper-level deduplication
across runs; repeated messages may produce repeated reports.

Optional `local_env.sh` settings (use your own credentials locally):

```bash
PAPER_APP_ID='cli_your_app_id'
PAPER_APP_SECRET='your_app_secret'
CODEX_PROXY_BASE_URL='http://127.0.0.1:8787/v1'
CODEX_PROXY_API_KEY='codex-proxy'
AI_MODEL='gpt-6-astra'
# Optional HTTP CONNECT proxy for paper downloads and HTTP(S) SDK traffic:
# LOCAL_HTTP_PROXY='http://127.0.0.1:7890'
```

If your proxy requires authentication, replace the placeholder with its key.
If it exposes Astra under a different model ID, set `AI_MODEL` to that ID.
Keep `local_env.sh` private; Git ignores it. These values are only loaded when
you run the launcher. `.env.example` documents variable names, and `.env` is
not loaded automatically.

The Python client also accepts the reference script's `AI_BASE_URL` (full
endpoint or `/v1` base), `AI_API_KEY`, and `AI_MODEL`. Without these, it accepts
`CODEX_PROXY_URL`/`CODEX_PROXY_BASE_URL` and `CODEX_PROXY_API_KEY`, or
`OPENAI_URL`/`OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `OPENAI_MODEL`. Explicit
`AI_*` values take precedence. Keep localhost out of any outbound HTTP proxy;
the launcher adds it to `NO_PROXY`, and AI requests connect directly to the
configured API endpoint.

Links are extracted from text and rich message content. The worker fetches
readable HTML, plain text, or PDF text **before** asking the model to analyze
it. arXiv links try the full HTML paper, then PDF, then the original page.
PDF extraction uses `pypdf`; scanned PDFs require OCR and are reported as
unreadable. Available paper PDFs are also saved under `papers/`, even when HTML
was used for reading. Filenames use publication year and title, for example
`2023-mimicplay-long-horizon-imitation-learning-by-watching-human-play.pdf`.
arXiv PDFs, direct PDFs, citation metadata, and up to 10 explicitly labeled main
Paper/PDF/Technical Report links per source are supported; bibliography references are not
downloaded indiscriminately. A blog without an associated paper stays a blog.
Unknown or invalid publication years use the current year in Asia/Shanghai;
missing titles fall back to the
PDF URL's filename. Identical PDFs reuse the existing file; different PDFs with
the same year/title get a content-hash suffix. Writes are atomic, owner-only,
and never overwrite an existing PDF. Download failures print a warning and are
recorded in the summary's `.json` diagnostics without blocking summarization.
Feishu file attachments without public links are not downloaded.

Each research message produces a timestamped Markdown summary and JSON
diagnostics, or an explicit failure explanation. If no source
can be read, the summarization AI is not called. Title-only discovery may still
call the AI to find a source. API/authentication failures leave a failure
report while raw message monitoring continues. The model is instructed to
distinguish claims from evidence, cite the supplied sources, and flag missing,
abstract-only or truncated material. Source pages are treated as untrusted data.
The AI request contains that message's content and extracted sources. Live
monitoring sends only complete, valid summaries as replies; failure reports
remain local. Reply failures print the API code or a sanitized connection error
and preserve the local report. Replay and standalone test runs never send messages.

Source downloads use 30-second socket timeouts, up to five redirects, a 20 MiB
download limit, and public addresses only. DNS results are validated and pinned;
each redirect is checked again. `LOCAL_HTTP_PROXY`, when configured, uses HTTP
CONNECT to the pinned address. Other HTTP proxy environment variables are for
SDK traffic; paper downloads use `LOCAL_HTTP_PROXY` explicitly. Each source is
limited to 100,000 characters and PDFs to 150 pages. A message is limited to
10 fetched links and 300,000 source characters; skipped or truncated content
is identified in the report. AI requests have a 30-minute timeout.

One worker processes up to 100 queued messages. If the queue fills, a warning
is printed and the raw message remains saved. Queued analysis is in memory;
after a crash or forced exit it is not replayed automatically. Ctrl+C finishes
queued work; pressing it again exits immediately.

Messages are automatically appended to `messages.txt` without overwriting
earlier entries. To choose another local file:

```bash
.venv/bin/python monitor.py --output paper-messages.txt
```

The default group name must match exactly. Duplicate names cause an error;
select the intended group explicitly using its chat ID:

```bash
.venv/bin/python monitor.py --chat-id oc_your_group_id
```

Messages from the selected ordinary group or topic group are written. Other groups and private
messages are ignored. Text messages are saved without their JSON wrapper,
preserving Unicode and line breaks. Other message types and unparseable text
are printed and saved as the raw `content` string from Feishu. Files/images
remain Feishu resource keys and are not downloaded. Sender metadata and the
outer event JSON are not included. Each message has a trailing newline.
New output files are created with owner-only read/write permissions; existing
files keep their permissions. `messages.txt` is ignored by Git; if you choose
a different filename, keep it out of version control too.

At startup, the monitor fetches the latest **3 history messages** visible to
the app in the selected chat, prints and appends them oldest first, then listens
for new events. History messages do not trigger paper analysis. Enable
`im:message.history:readonly` and `im:message.group_msg`, then publish and approve
the app version. A history API failure prints a warning and live monitoring
continues. The chat history API may return topic roots rather than all replies;
this fetch does not crawl individual threads. Restarting appends the history
again because deduplication is limited to the current process.

This does not monitor edits or deletions. Feishu controls which message sources and types are
eligible for event delivery; this does not guarantee receipt of messages from
other bots or recovery of events missed while offline. The SDK handles
reconnection. Duplicate suppression covers the most recent 10,000 message IDs
in one process and resets when restarted. Run one listener instance.

## Startup error 232034

Feishu reports this when the app is not installed or enabled in the current
tenant. In **版本管理与发布**, create and publish a version, complete any required
approval, and have a tenant administrator install/enable the app if needed.
An app marked **待上线** is not yet published. After activation, add its bot to
**paper分享** and restart the listener. Granting API scopes alone does not
install or activate the app.

## Offline tests

Tests use only dummy configuration, local event fixtures, and mocked network
responses; they do not load your credentials or connect to Feishu or your AI
proxy. The launcher is tested in a temporary directory with dummy settings.
Install requirements to include the generated-PDF extraction test; that test
is skipped if `pypdf` is absent.

```bash
python3 -m unittest -v
```

References: [custom webhook bot limitations](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot),
[message events](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/events/receive),
[official Python WebSocket example](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/samples/ws/sample.py).
AI references: [GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra),
[Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).
