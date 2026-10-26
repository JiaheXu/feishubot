"""Bounded, read-only source discovery using the analyzer's AI connection."""

import json

from paper_sources import arxiv_id, extract_links, fetch_source, search_papers, search_web


TOOLS = [{"type": "function", "function": {
    "name": name, "description": description,
    "parameters": {"type": "object", "properties": {argument: {"type": "string"}},
                   "required": [argument], "additionalProperties": False},
}} for name, argument, description in (
    ("search_papers", "query", "Search arXiv metadata. Use arXiv syntax such as ti:\"paper title\" or all:robotics."),
    ("search_web", "query", "Search public web pages by title, author or organization. May be unavailable."),
    ("read_source", "url", "Read a public source URL; arXiv automatically tries full HTML/PDF before the abstract."),
)]


def discover(analyzer, content):
    prompt = analyzer.discovery_prompt_path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise RuntimeError("The discovery prompt file is empty.")
    retrieved = {}
    attempted = set()
    budget = 300_000

    def read_source(url):
        nonlocal budget
        if not isinstance(url, str) or len(url) > 4096:
            raise ValueError("Source URL must be a string of at most 4096 characters.")
        if url in retrieved:
            return retrieved[url]
        if url not in attempted and len(attempted) >= 10:
            raise RuntimeError("Discovery source limit reached (10 links).")
        if budget <= 0:
            raise RuntimeError("Discovery source text budget exhausted.")
        attempted.add(url)
        result = fetch_source(url)
        if len(result["text"]) > budget:
            result["text"] = result["text"][:budget]
            result["truncated"] = True
        budget -= len(result["text"])
        retrieved[url] = result
        return result

    supplied_sources = []
    pending_urls = extract_links(content)[:10]
    for url in pending_urls:
        try:
            source = read_source(url)
            supplied_sources.append(source)
            # Follow main-paper links through project pages within the same bounded queue.
            if not (arxiv_id(url) or arxiv_id(source.get("retrieved_url", ""))):
                for paper_url in [source.get("pdf_url"), *(source.get("paper_urls") or [])]:
                    if (paper_url and paper_url not in pending_urls
                            and paper_url != source.get("retrieved_url") and len(pending_urls) < 10):
                        pending_urls.append(paper_url)
        except Exception as error:
            supplied_sources.append({"url": url, "error": str(error)})
    user_content = json.dumps({"message": content[:12_000], "supplied_sources": supplied_sources},
                              ensure_ascii=False) if supplied_sources else content[:12_000]
    messages = [{"role": "system", "content": prompt},
                {"role": "user", "content": user_content}]
    calls = 0
    for _ in range(9):
        reply = analyzer._chat(messages, tools=TOOLS)
        messages.append(reply)
        tool_calls = reply.get("tool_calls") or []
        if not tool_calls:
            decision = json.loads(reply.get("content") or "{}")
            if not isinstance(decision, dict) or decision.get("status") not in (
                    "verified", "ambiguous", "not_research", "unavailable"):
                raise RuntimeError("Discovery returned an invalid decision.")
            reason = decision.get("reason")
            if not isinstance(reason, str):
                raise RuntimeError("Discovery returned no explanation.")
            if decision["status"] != "verified":
                return [], f"{decision['status']}: {reason}"
            urls = decision.get("urls")
            if not isinstance(urls, list) or not urls or any(
                    not isinstance(url, str) or url not in retrieved for url in urls):
                raise RuntimeError("Discovery selected a source that was not retrieved and verified.")
            return [retrieved[url] for url in dict.fromkeys(urls)], reason
        for call in tool_calls:
            calls += 1
            if calls > 8:
                raise RuntimeError("Discovery tool-call limit reached; no verified summary requested.")
            try:
                function = call["function"]
                args = json.loads(function["arguments"])
                name = function["name"]
                if name in ("search_web", "search_papers"):
                    query = args["query"]
                    if not isinstance(query, str) or not query.strip() or len(query) > 500:
                        raise ValueError("Search query must be a nonempty string of at most 500 characters.")
                    result = (search_papers if name == "search_papers" else search_web)(query)
                elif name == "read_source":
                    result = read_source(args["url"])
                else:
                    raise ValueError("Unknown discovery tool.")
            except Exception as error:
                result = {"error": str(error)}
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(result, ensure_ascii=False)})
    raise RuntimeError("Discovery turn limit reached.")
