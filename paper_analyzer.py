"""Analyze linked documents through an OpenAI-compatible local proxy."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import queue
import re
import sys
import threading
import urllib.error
import urllib.request

from paper_discovery import discover
from paper_sources import archive_papers, extract_links


DEFAULT_PROMPT = Path(__file__).with_name("paper_prompt.txt")
DISCOVERY_PROMPT = Path(__file__).with_name("discovery_prompt.txt")
MAX_SUMMARY_CHARS = 1000
SUMMARY_HEADER = "本摘要由 gpt-6-astra 生成"
SUMMARY_HEADER_RE = re.compile(r"^本摘要由 [^\r\n]+ 生成(?:\n|$)")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PaperAnalyzer:
    def __init__(self, endpoint="http://127.0.0.1:8787/v1/chat/completions",
                 model="gpt-6-astra", api_key="codex-proxy", prompt_path=DEFAULT_PROMPT,
                 output_dir="analyses", timeout=1800, discovery_prompt_path=DISCOVERY_PROMPT,
                 papers_dir="papers"):
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.prompt_path = Path(prompt_path)
        self.output_dir = Path(output_dir)
        self.timeout = timeout
        self.discovery_prompt_path = Path(discovery_prompt_path)
        self.papers_dir = Path(papers_dir)

    @classmethod
    def from_env(cls, env):
        endpoint = (env.get("AI_BASE_URL") or env.get("CODEX_PROXY_URL")
                    or env.get("OPENAI_URL") or env.get("CODEX_PROXY_BASE_URL")
                    or env.get("OPENAI_BASE_URL") or "http://127.0.0.1:8787/v1")
        endpoint = endpoint.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        return cls(endpoint=endpoint,
                   model=env.get("AI_MODEL") or env.get("OPENAI_MODEL") or "gpt-6-astra",
                   api_key=env.get("AI_API_KEY") or env.get("CODEX_PROXY_API_KEY")
                   or env.get("OPENAI_API_KEY") or "codex-proxy")

    def _chat(self, messages, tools=None):
        payload = {"model": self.model, "stream": False, "messages": messages}
        if tools:
            payload["tools"] = tools
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.endpoint,
                                         data=json.dumps(payload).encode(), headers=headers)
        # The AI endpoint is explicitly configured; do not route localhost via HTTP_PROXY.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=self.timeout) as response:
                body = response.read(4 * 1024 * 1024 + 1)
            if len(body) > 4 * 1024 * 1024:
                raise RuntimeError("AI response exceeds 4 MiB.")
            data = json.loads(body)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"AI proxy returned HTTP {error.code}; check the proxy, model and authentication.") from None
        except (OSError, urllib.error.URLError):
            raise RuntimeError("AI proxy connection failed or timed out; check that the local proxy is running.") from None
        try:
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length":
                raise RuntimeError("AI output was truncated; no complete analysis was returned.")
            message = choice["message"]
            if not isinstance(message, dict):
                raise TypeError("Invalid assistant message")
            text = message.get("content")
            if not message.get("tool_calls") and (not isinstance(text, str) or not text.strip()):
                raise RuntimeError("AI proxy returned no analysis text.")
            model = data.get("model")
            if isinstance(model, str) and model.strip() and not any(c in model for c in "\r\n"):
                message["_model"] = model.strip()
            return message
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("AI proxy returned an invalid Chat Completions response.") from None

    def _complete(self, sources, content):
        prompt = self.prompt_path.read_text(encoding="utf-8").strip().replace("<模型名称>", self.model)
        if not prompt:
            raise RuntimeError("The paper analysis prompt file is empty.")
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps({
                "message": content[:12_000],
                "paper_urls": [source["url"] for source in sources if source.get("text")],
                "sources": sources,
            }, ensure_ascii=False)},
        ]
        for _ in range(2):
            reply = self._chat(messages)
            text = reply.get("content")
            if not isinstance(text, str) or not text.strip():
                raise RuntimeError("AI proxy returned no analysis text.")
            model = reply.get("_model") or self.model
            text = f"本摘要由 {model} 生成\n" + SUMMARY_HEADER_RE.sub("", text.strip(), count=1)
            if len(text) <= MAX_SUMMARY_CHARS:
                return text
            messages.extend([{"role": "assistant", "content": text},
                             {"role": "user", "content": json.dumps({"length_error": {
                                 "characters": len(text),
                                 "max_characters": MAX_SUMMARY_CHARS}})}])
        raise RuntimeError("Summary still exceeds the single-message limit after shortening.")

    def analyze(self, message):
        content = message.get("content") or ""
        urls = extract_links(content)
        try:
            sources, discovery = discover(self, content)
        except Exception as error:
            sources, discovery = [], f"Discovery failed: {error}"
        if not sources and discovery.startswith("not_research:"):
            return None
        if not sources:
            # Keep archiving supplied PDFs even when discovery cannot read them.
            sources = [{"url": url, "error": discovery} for url in urls[:10]]
        for source in sources:
            source["pdf_files"], source["pdf_errors"] = archive_papers(source, self.papers_dir)
            for error in source["pdf_errors"]:
                print(f"PDF archive warning: {error}", file=sys.stderr, flush=True)
        if not any(source.get("text") for source in sources):
            result = f"Analysis unavailable: no readable source was retrieved. {discovery}".strip()
        else:
            try:
                result = self._complete(sources, content)
            except Exception as error:
                result = f"Analysis unavailable: {error}"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        identifier = hashlib.sha256(message.get("message_id", "unknown").encode()).hexdigest()[:12]
        self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.output_dir / f"{stamp}_{identifier}.md"
        details = {"model": self.model, "message_id": message.get("message_id"),
                   "discovery": discovery,
                   "sources": [{key: value for key, value in source.items() if key != "text"}
                               for source in sources]}
        with path.with_suffix(".json").open("x", encoding="utf-8") as output:
            path.with_suffix(".json").chmod(0o600)
            json.dump(details, output, ensure_ascii=False, indent=2)
        with path.open("x", encoding="utf-8") as output:
            path.chmod(0o600)
            output.write(result.strip() + "\n")
        return path


class AnalysisWorker:
    def __init__(self, analyzer, output=None, reply=None):
        self.analyzer = analyzer
        self.reply = reply
        self.output = output if output is not None else sys.stderr
        self.jobs = queue.Queue(maxsize=100)
        self.thread = threading.Thread(target=self._run, name="paper-analysis", daemon=True)
        self.thread.start()

    def submit(self, message):
        if not (message.get("content") or "").strip():
            return False
        try:
            self.jobs.put_nowait(message)
            return True
        except queue.Full:
            print("Analysis queue is full; this message remains in the raw log but was not queued.", file=self.output, flush=True)
            return False

    def _run(self):
        while True:
            message = self.jobs.get()
            try:
                if message is None:
                    return
                path = self.analyzer.analyze(message)
                if path is None:
                    print("Skipped: message does not identify a paper/report.", file=self.output, flush=True)
                    continue
                summary = path.read_text(encoding="utf-8").strip()
                print(summary, file=self.output, flush=True)
                print(f"Analysis report saved: {path}", file=self.output, flush=True)
                if self.reply is not None and not summary.startswith("Analysis unavailable:"):
                    try:
                        self.reply(message, summary)
                        print("Summary replied to the original message.", file=self.output, flush=True)
                    except Exception as error:
                        print(f"Summary reply failed: {error}; local report retained: {path}",
                              file=self.output, flush=True)
            except Exception as error:
                print(f"Paper analysis failed: {error}", file=self.output, flush=True)
            finally:
                self.jobs.task_done()

    def close(self):
        self.jobs.put(None)
        self.thread.join()
