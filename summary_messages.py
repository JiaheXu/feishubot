"""Generate and check Chinese summaries from saved messages, without Feishu."""

import argparse
import json
import os
from pathlib import Path

from paper_analyzer import MAX_SUMMARY_CHARS, PaperAnalyzer


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("messages", nargs="?", type=Path,
                        default=Path(__file__).with_name("examples") / "history_messages.json")
    parser.add_argument("--output-dir", type=Path, default=Path("analyses/message_tests"))
    args = parser.parse_args(argv)
    messages = json.loads(args.messages.read_text(encoding="utf-8"))
    if not isinstance(messages, list) or not messages or any(
            not isinstance(message, dict) or not isinstance(message.get("content"), str)
            or not message["content"].strip() for message in messages):
        raise ValueError("Expected a nonempty JSON list of messages with nonempty string content.")
    analyzer = PaperAnalyzer.from_env(os.environ)
    analyzer.output_dir = args.output_dir
    failures = 0
    for index, message in enumerate(messages, 1):
        print(f"Message {index}/{len(messages)}", flush=True)
        try:
            path = analyzer.analyze(message)
            if path is None:
                print("SKIP: message does not identify a paper/report.\n", flush=True)
                continue
            summary = path.read_text(encoding="utf-8").strip()
            print(summary, flush=True)
            failed = (summary.startswith("Analysis unavailable:") or not summary
                      or len(summary) > MAX_SUMMARY_CHARS
                      or any(label not in summary for label in ("问题：", "创新：", "方法：", "结果：", "局限性（AI 分析）：")))
            print(f"{'FAIL' if failed else 'PASS'}: {path}\n", flush=True)
            failures += failed
        except Exception as error:
            failures += 1
            print(f"FAIL: {error}\n", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
