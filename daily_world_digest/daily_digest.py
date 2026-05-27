#!/usr/bin/env python3
"""
Collect trending world events from public feeds, add continuity from recent history,
and email a source-linked daily digest.
"""

from __future__ import annotations

import argparse
import dataclasses
import email.utils
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any


STOPWORDS = {
    "about", "after", "again", "against", "amid", "among", "and", "are", "around",
    "as", "at", "back", "be", "been", "before", "being", "between", "but", "by",
    "can", "could", "did", "do", "does", "during", "for", "from", "had", "has",
    "have", "he", "her", "his", "how", "in", "into", "is", "it", "its", "more",
    "new", "news", "not", "of", "on", "or", "over", "says", "she", "that", "the",
    "their", "they", "this", "to", "under", "up", "was", "were", "what", "when",
    "where", "which", "while", "who", "why", "will", "with", "you", "your",
}


@dataclasses.dataclass(frozen=True)
class Article:
    id: str
    title: str
    link: str
    source: str
    region: str
    published: str
    summary: str
    keywords: list[str]


@dataclasses.dataclass
class Event:
    title: str
    keywords: list[str]
    articles: list[Article]
    previous_titles: list[str]


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            print(f"warning: ignoring malformed .env line {line_number}", file=sys.stderr)
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            print(f"warning: ignoring invalid .env key on line {line_number}: {key}", file=sys.stderr)
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def article_id(link: str, title: str) -> str:
    raw = (link or title).encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()[:16]


def keywords_for(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", text.lower())
    filtered = [w.strip("-'") for w in words if w not in STOPWORDS and len(w) > 2]
    counts = Counter(filtered)
    return [word for word, _ in counts.most_common(12)]


def parse_date(value: str | None) -> str:
    if not value:
        return datetime.now(timezone.utc).isoformat()
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat()


def fetch_url(url: str, timeout: int = 20) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "file":
        return Path(urllib.request.url2pathname(parsed.path)).read_bytes()
    if not parsed.scheme:
        return Path(url).read_bytes()

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "daily-world-digest/1.0 (+https://example.local)",
            "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def parse_feed(feed: dict[str, str]) -> list[Article]:
    raw = fetch_url(feed["url"])
    root = ET.fromstring(raw)
    items = root.findall(".//item")
    if not items:
        items = root.findall("{http://www.w3.org/2005/Atom}entry")

    articles: list[Article] = []
    for item in items[:40]:
        title = clean_text(_first_text(item, ["title", "{http://www.w3.org/2005/Atom}title"]))
        link = _first_text(item, ["link"])
        atom_link = item.find("{http://www.w3.org/2005/Atom}link")
        if not link and atom_link is not None:
            link = atom_link.attrib.get("href", "")
        summary = clean_text(
            _first_text(
                item,
                [
                    "description",
                    "summary",
                    "{http://www.w3.org/2005/Atom}summary",
                    "{http://search.yahoo.com/mrss/}description",
                ],
            )
        )
        published = parse_date(
            _first_text(
                item,
                [
                    "pubDate",
                    "published",
                    "updated",
                    "{http://www.w3.org/2005/Atom}published",
                    "{http://www.w3.org/2005/Atom}updated",
                ],
            )
        )
        if not title or not link:
            continue
        text = f"{title} {summary}"
        articles.append(
            Article(
                id=article_id(link, title),
                title=title,
                link=link,
                source=feed["name"],
                region=feed.get("region", "world"),
                published=published,
                summary=summary,
                keywords=keywords_for(text),
            )
        )
    return articles


def _first_text(item: ET.Element, names: list[str]) -> str:
    for name in names:
        child = item.find(name)
        if child is not None and child.text:
            return child.text.strip()
    return ""


def collect_articles(config: dict[str, Any]) -> list[Article]:
    articles: list[Article] = []
    seen: set[str] = set()
    for feed in config["feeds"]:
        try:
            for article in parse_feed(feed):
                if article.id not in seen:
                    articles.append(article)
                    seen.add(article.id)
        except (ET.ParseError, urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"warning: failed to fetch {feed['name']}: {exc}", file=sys.stderr)
    articles.sort(key=lambda item: item.published, reverse=True)
    return articles


def jaccard(left: list[str], right: list[str]) -> float:
    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cluster_articles(articles: list[Article]) -> list[list[Article]]:
    clusters: list[list[Article]] = []
    for article in articles:
        placed = False
        for cluster in clusters:
            if jaccard(article.keywords, cluster[0].keywords) >= 0.18:
                cluster.append(article)
                placed = True
                break
        if not placed:
            clusters.append([article])
    clusters.sort(key=lambda cluster: (len({a.source for a in cluster}), len(cluster)), reverse=True)
    return clusters


def load_history(path: Path, days: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            created = datetime.fromisoformat(row["created_at"])
            if created >= cutoff:
                rows.append(row)
        except (KeyError, ValueError, json.JSONDecodeError):
            continue
    return rows


def build_events(config: dict[str, Any], articles: list[Article], history: list[dict[str, Any]]) -> list[Event]:
    max_events = int(config["digest"].get("max_events", 12))
    min_sources = int(config["digest"].get("min_sources_per_event", 1))
    events: list[Event] = []
    for cluster in cluster_articles(articles):
        sources = {article.source for article in cluster}
        if len(sources) < min_sources:
            continue
        title = cluster[0].title
        keywords = keywords_for(" ".join(article.title for article in cluster))
        previous = related_history_titles(keywords, history)
        events.append(Event(title=title, keywords=keywords, articles=cluster[:5], previous_titles=previous))
        if len(events) >= max_events:
            break
    return events


def related_history_titles(keywords: list[str], history: list[dict[str, Any]]) -> list[str]:
    related: list[tuple[float, str]] = []
    for row in history:
        score = jaccard(keywords, row.get("keywords", []))
        if score >= 0.18:
            related.append((score, row.get("title", "")))
    related.sort(reverse=True)
    titles: list[str] = []
    for _, title in related:
        if title and title not in titles:
            titles.append(title)
        if len(titles) >= 3:
            break
    return titles


def event_context(event: Event) -> str:
    summaries = [article.summary for article in event.articles if article.summary]
    if event.previous_titles:
        before = "；".join(event.previous_titles)
        return f"此前相关进展包括：{before}。今天的新报道集中在：{summaries[0] if summaries else event.title}"
    if summaries:
        return f"目前可见的背景是：{summaries[0]}"
    return "该事件今天首次进入简报候选；建议继续观察后续报道来建立更完整的时间线。"


def render_html(config: dict[str, Any], events: list[Event], now: datetime) -> str:
    title = config["digest"].get("title", "Daily World Digest")
    date_label = now.strftime("%Y-%m-%d")
    parts = [
        "<!doctype html><html><body>",
        f"<h1>{html.escape(title)} - {date_label}</h1>",
        "<p>这是一份自动生成的世界事件简报。每个事件都保留来源链接，并结合最近历史记录给出连续性背景。</p>",
    ]
    for idx, event in enumerate(events, start=1):
        parts.append(f"<h2>{idx}. {html.escape(event.title)}</h2>")
        parts.append(f"<p><strong>前因后果：</strong>{html.escape(event_context(event))}</p>")
        parts.append("<p><strong>来源：</strong></p><ul>")
        for article in event.articles:
            source = html.escape(article.source)
            title = html.escape(article.title)
            link = html.escape(article.link, quote=True)
            published = html.escape(article.published[:10])
            parts.append(f'<li>{source}, {published}: <a href="{link}">{title}</a></li>')
        parts.append("</ul>")
    parts.append("</body></html>")
    return "\n".join(parts)


def render_text(config: dict[str, Any], events: list[Event], now: datetime) -> str:
    title = config["digest"].get("title", "Daily World Digest")
    lines = [f"{title} - {now:%Y-%m-%d}", ""]
    for idx, event in enumerate(events, start=1):
        lines.append(f"{idx}. {event.title}")
        lines.append(f"前因后果：{event_context(event)}")
        lines.append("来源：")
        for article in event.articles:
            lines.append(f"- {article.source}, {article.published[:10]}: {article.title} ({article.link})")
        lines.append("")
    return "\n".join(lines)


def send_email(config: dict[str, Any], subject: str, text_body: str, html_body: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ["SMTP_USERNAME"]
    password = os.environ["SMTP_PASSWORD"]

    message = EmailMessage()
    message["From"] = config["email"]["from"]
    message["To"] = ", ".join(config["email"]["to"])
    message["Subject"] = subject
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port) as smtp:
            smtp.starttls(context=context)
            smtp.login(username, password)
            smtp.send_message(message)


def append_history(path: Path, events: list[Event], now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for event in events:
            row = {
                "created_at": now.astimezone(timezone.utc).isoformat(),
                "title": event.title,
                "keywords": event.keywords,
                "sources": [article.source for article in event.articles],
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_output(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    parser = argparse.ArgumentParser(description="Generate and email a daily world news digest.")
    parser.add_argument("--config", default="config.json", help="Path to config JSON.")
    parser.add_argument("--env", default=".env", help="Path to SMTP environment file.")
    parser.add_argument("--history", default="data/history.jsonl", help="Path to local event history JSONL.")
    parser.add_argument("--output-html", default="data/latest_digest.html", help="Where to write the HTML digest.")
    parser.add_argument("--output-text", default="data/latest_digest.txt", help="Where to write the plain-text digest.")
    parser.add_argument("--send", action="store_true", help="Send the digest by SMTP.")
    args = parser.parse_args()

    load_env_file(Path(args.env))
    config = load_config(Path(args.config))
    now = datetime.now(timezone.utc)
    articles = collect_articles(config)
    if not articles:
        print("No articles collected; not sending an empty digest.", file=sys.stderr)
        return 2

    history = load_history(Path(args.history), int(config["digest"].get("history_days", 21)))
    events = build_events(config, articles, history)
    html_body = render_html(config, events, now)
    text_body = render_text(config, events, now)
    write_output(Path(args.output_html), html_body)
    write_output(Path(args.output_text), text_body)
    append_history(Path(args.history), events, now)

    if args.send:
        prefix = config["email"].get("subject_prefix", "[World Digest]")
        send_email(config, f"{prefix} {now:%Y-%m-%d}", text_body, html_body)
        print(f"Sent {len(events)} events.")
    else:
        print(textwrap.shorten(text_body.replace("\n", " "), width=220))
        print(f"Wrote {args.output_html} and {args.output_text}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
