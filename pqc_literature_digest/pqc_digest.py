#!/usr/bin/env python3
"""Daily bilingual PQC literature digest.

The script intentionally uses the Python standard library only, so it can run
from Windows Task Scheduler without a dependency installation step.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import email.message
import html
import json
import os
import re
import smtplib
import ssl
import sys
import textwrap
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
USER_AGENT = "pqc-literature-digest/0.1 (mailto:replace-with-your-email)"


@dataclass
class Paper:
    title: str
    authors: list[str] = field(default_factory=list)
    abstract: str = ""
    url: str = ""
    source: str = ""
    published: str = ""
    venue: str = ""
    doi: str = ""
    labels: list[str] = field(default_factory=list)
    score: int = 0
    zh_summary: str = ""
    en_summary: str = ""
    relevance_zh: str = ""
    relevance_en: str = ""

    @property
    def key(self) -> str:
        if self.doi:
            return f"doi:{self.doi.lower()}"
        if self.url:
            return f"url:{self.url.lower().rstrip('/')}"
        return f"title:{normalize_text(self.title)}"


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def keyword_in_text(keyword: str, original_text: str, normalized_text: str) -> bool:
    clean_keyword = keyword.strip()
    if clean_keyword.upper() == "NTT":
        if re.search(r"\bNTT\s+DATA\b", original_text, flags=re.I):
            return False
        return bool(re.search(r"\bNTT\b", original_text))
    if len(clean_keyword) <= 6 and re.fullmatch(r"[A-Za-z0-9+.-]+", clean_keyword):
        return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(clean_keyword)}(?![A-Za-z0-9])", original_text, flags=re.I))
    return normalize_text(clean_keyword) in normalized_text


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def http_get(url: str, params: dict[str, Any] | None = None, accept: str = "*/*") -> bytes:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def http_post_json(url: str, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_date(value: str) -> dt.date | None:
    if not value:
        return None
    value = value.strip()
    try:
        return email.utils.parsedate_to_datetime(value).date()
    except (TypeError, ValueError, IndexError):
        pass
    for pattern in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return dt.datetime.strptime(value[:10] if pattern == "%Y-%m-%d" else value, pattern).date()
        except ValueError:
            pass
    match = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", value)
    if match:
        return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return None


def clean_xml_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def fetch_iacr(config: dict[str, Any]) -> list[Paper]:
    source_cfg = config["sources"]["iacr_eprint"]
    papers: list[Paper] = []
    for url in source_cfg.get("rss_candidates", []):
        try:
            raw = http_get(url, accept="application/rss+xml, application/xml, text/html")
        except Exception:
            continue
        text = raw.decode("utf-8", errors="replace")
        parsed = parse_rss(text, "IACR ePrint")
        if parsed:
            return parsed
        parsed = parse_iacr_homepage(text)
        if parsed:
            papers.extend(parsed)
            break
    return papers


def parse_rss(text: str, source: str) -> list[Paper]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    papers: list[Paper] = []
    for item in root.findall(".//item"):
        title = clean_xml_text(item.findtext("title"))
        link = clean_xml_text(item.findtext("link"))
        description = clean_xml_text(item.findtext("description"))
        pub_date = clean_xml_text(item.findtext("pubDate"))
        if title:
            papers.append(Paper(title=title, abstract=description, url=link, source=source, published=pub_date))
    return papers


def parse_iacr_homepage(text: str) -> list[Paper]:
    papers: list[Paper] = []
    blocks = re.split(r"(?=<h[2-6][^>]*>)", text, flags=re.I)
    for block in blocks:
        id_match = re.search(r"(20\d{2}/\d{3,5})", block)
        title_match = re.search(r"<h[2-6][^>]*>\s*(.*?)\s*</h[2-6]>", block, flags=re.I | re.S)
        if not id_match or not title_match:
            continue
        title = clean_xml_text(re.sub(r"<[^>]+>", " ", title_match.group(1)))
        paper_id = id_match.group(1)
        papers.append(Paper(title=title, url=f"https://eprint.iacr.org/{paper_id}", source="IACR ePrint"))
    return papers


def fetch_arxiv(config: dict[str, Any]) -> list[Paper]:
    papers: list[Paper] = []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for query in config["sources"]["arxiv"].get("queries", []):
        params = {
            "search_query": query,
            "start": 0,
            "max_results": 25,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        try:
            root = ET.fromstring(http_get("https://export.arxiv.org/api/query", params).decode("utf-8"))
        except Exception:
            continue
        for entry in root.findall("atom:entry", ns):
            title = clean_xml_text(entry.findtext("atom:title", namespaces=ns))
            abstract = clean_xml_text(entry.findtext("atom:summary", namespaces=ns))
            url = clean_xml_text(entry.findtext("atom:id", namespaces=ns))
            published = clean_xml_text(entry.findtext("atom:published", namespaces=ns))
            authors = [clean_xml_text(a.findtext("atom:name", namespaces=ns)) for a in entry.findall("atom:author", ns)]
            if title:
                papers.append(Paper(title=title, authors=authors, abstract=abstract, url=url, source="arXiv", published=published))
        time.sleep(3)
    return papers


def fetch_dblp(config: dict[str, Any]) -> list[Paper]:
    papers: list[Paper] = []
    for query in config["sources"]["dblp"].get("queries", []):
        params = {"q": query, "format": "json", "h": 30}
        try:
            data = json.loads(http_get("https://dblp.org/search/publ/api", params).decode("utf-8"))
        except Exception:
            continue
        hits = data.get("result", {}).get("hits", {}).get("hit", [])
        for hit in hits:
            info = hit.get("info", {})
            authors = info.get("authors", {}).get("author", [])
            if isinstance(authors, dict):
                authors = [authors.get("text", "")]
            elif isinstance(authors, list):
                authors = [a.get("text", a) if isinstance(a, dict) else a for a in authors]
            papers.append(
                Paper(
                    title=clean_xml_text(info.get("title", "")),
                    authors=[a for a in authors if a],
                    url=info.get("url", ""),
                    source="DBLP",
                    published=str(info.get("year", "")),
                    venue=info.get("venue", ""),
                    doi=info.get("doi", ""),
                )
            )
    return papers


def fetch_crossref(config: dict[str, Any]) -> list[Paper]:
    papers: list[Paper] = []
    mailto = config["sources"]["crossref"].get("mailto", "")
    from_date = (dt.date.today() - dt.timedelta(days=config["digest"].get("lookback_days", 2))).isoformat()
    for query in config["sources"]["crossref"].get("queries", []):
        params = {
            "query": query,
            "rows": 25,
            "sort": "published",
            "order": "desc",
            "filter": f"from-pub-date:{from_date},type:journal-article",
        }
        if mailto:
            params["mailto"] = mailto
        try:
            data = json.loads(http_get("https://api.crossref.org/works", params).decode("utf-8"))
        except Exception:
            continue
        for item in data.get("message", {}).get("items", []):
            title = " ".join(item.get("title", [])).strip()
            authors = [
                " ".join(part for part in [a.get("given", ""), a.get("family", "")] if part).strip()
                for a in item.get("author", [])
            ]
            date_parts = item.get("published-print", item.get("published-online", item.get("created", {}))).get("date-parts", [[]])[0]
            published = "-".join(str(x) for x in date_parts) if date_parts else ""
            papers.append(
                Paper(
                    title=title,
                    authors=[a for a in authors if a],
                    abstract=clean_xml_text(re.sub(r"<[^>]+>", " ", item.get("abstract", ""))),
                    url=item.get("URL", ""),
                    source="Crossref",
                    published=published,
                    venue=" ".join(item.get("container-title", [])),
                    doi=item.get("DOI", ""),
                )
            )
    return papers


def fetch_pubmed(config: dict[str, Any]) -> list[Paper]:
    papers: list[Paper] = []
    for query in config["sources"]["pubmed"].get("queries", []):
        try:
            search = json.loads(
                http_get(
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                    {"db": "pubmed", "term": query, "retmode": "json", "retmax": 20, "sort": "pub date"},
                ).decode("utf-8")
            )
            ids = search.get("esearchresult", {}).get("idlist", [])
            if not ids:
                continue
            summary = json.loads(
                http_get(
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                    {"db": "pubmed", "id": ",".join(ids), "retmode": "json"},
                ).decode("utf-8")
            )
        except Exception:
            continue
        result = summary.get("result", {})
        for pmid in result.get("uids", []):
            item = result.get(pmid, {})
            authors = [a.get("name", "") for a in item.get("authors", [])]
            papers.append(
                Paper(
                    title=item.get("title", ""),
                    authors=[a for a in authors if a],
                    url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    source="PubMed",
                    published=item.get("pubdate", ""),
                    venue=item.get("fulljournalname", ""),
                )
            )
    return papers


FETCHERS = {
    "iacr_eprint": fetch_iacr,
    "arxiv": fetch_arxiv,
    "dblp": fetch_dblp,
    "crossref": fetch_crossref,
    "pubmed": fetch_pubmed,
}


def within_lookback(paper: Paper, lookback_days: int) -> bool:
    parsed = parse_date(paper.published)
    if not parsed:
        if re.fullmatch(r"20\d{2}", paper.published.strip()):
            return paper.published.strip() == str(dt.date.today().year)
        return True
    return parsed >= dt.date.today() - dt.timedelta(days=lookback_days + 1)


def classify_and_score(paper: Paper, config: dict[str, Any]) -> None:
    profile = config["profile"]
    original_text = " ".join([paper.title, paper.abstract, paper.venue])
    text = normalize_text(original_text)
    score = 0
    labels: list[str] = []
    for keyword in profile.get("primary_keywords", []):
        if keyword_in_text(keyword, original_text, text):
            score += 5
    for keyword in profile.get("secondary_keywords", []):
        if keyword_in_text(keyword, original_text, text):
            score += 2
    for venue in profile.get("high_quality_venues", []):
        if keyword_in_text(venue, original_text, text):
            score += 4
    score += config["sources"].get(source_key(paper.source), {}).get("priority", 1)
    for label, keywords in profile.get("labels", {}).items():
        if any(keyword_in_text(keyword, original_text, text) for keyword in keywords):
            labels.append(label)
    if paper.source == "IACR ePrint":
        score += 3
    paper.labels = labels or ["密码学"]
    paper.score = score


def source_key(source: str) -> str:
    return {
        "IACR ePrint": "iacr_eprint",
        "arXiv": "arxiv",
        "DBLP": "dblp",
        "Crossref": "crossref",
        "PubMed": "pubmed",
    }.get(source, source)


def dedupe(papers: list[Paper]) -> list[Paper]:
    by_key: dict[str, Paper] = {}
    for paper in papers:
        if not paper.title:
            continue
        key = paper.key
        old = by_key.get(key)
        if not old or paper.score > old.score or (paper.abstract and not old.abstract):
            by_key[key] = paper
    return list(by_key.values())


def summarize_without_llm(paper: Paper) -> None:
    abstract = paper.abstract or "No abstract available from the metadata source."
    compact = textwrap.shorten(abstract, width=520, placeholder="...")
    paper.en_summary = compact
    paper.zh_summary = "未启用 LLM 双语摘要；以下条目的英文摘要来自元数据源，可在配置 OPENAI_API_KEY 后自动生成中文摘要。"
    if any(label in paper.labels for label in ["PQC", "NTT", "polynomial multiplication", "implementation"]):
        paper.relevance_en = "Likely relevant to PQC, implementation, or polynomial arithmetic based on matched title/abstract keywords."
        paper.relevance_zh = "标题或摘要命中了 PQC、实现或多项式算术相关关键词，建议优先浏览。"
    else:
        paper.relevance_en = "Matched the broader cryptography profile."
        paper.relevance_zh = "命中密码学相关关键词，可作为扩展阅读。"


def summarize_with_llm(papers: list[Paper], config: dict[str, Any]) -> None:
    llm_cfg = config.get("llm", {})
    endpoint = llm_cfg.get("endpoint", "")
    provider = llm_cfg.get("provider", "openai_compatible").lower()
    if provider == "deepseek" or "deepseek.com" in endpoint:
        api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
        model = os.environ.get("DEEPSEEK_MODEL") or llm_cfg.get("model", "deepseek-chat")
    else:
        api_key = os.environ.get("OPENAI_API_KEY")
        model = os.environ.get("OPENAI_MODEL") or llm_cfg.get("model", "gpt-4.1-mini")
    if not api_key or not llm_cfg.get("enabled"):
        for paper in papers:
            summarize_without_llm(paper)
        return

    endpoint = llm_cfg.get("endpoint", "https://api.openai.com/v1/responses")
    for paper in papers:
        prompt = {
            "title": paper.title,
            "authors": paper.authors,
            "venue": paper.venue,
            "source": paper.source,
            "abstract": paper.abstract,
            "labels": paper.labels,
        }
        try:
            if endpoint.rstrip("/").endswith("/chat/completions"):
                response = http_post_json(
                    endpoint,
                    {
                        "model": model,
                        "messages": [
                            {
                                "role": "system",
                                "content": "You summarize cryptography papers for a PQC PhD student. Return compact JSON only.",
                            },
                            {
                                "role": "user",
                                "content": (
                                    "Create bilingual summaries for this paper. JSON keys: "
                                    "en_summary, zh_summary, relevance_en, relevance_zh. "
                                    "Each summary should be 2-3 sentences, technical but readable.\n"
                                    + json.dumps(prompt, ensure_ascii=False)
                                ),
                            },
                        ],
                        "response_format": {"type": "json_object"},
                        "temperature": 0.2,
                    },
                    api_key,
                )
                content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
            else:
                response = http_post_json(
                    endpoint,
                    {
                        "model": model,
                        "input": [
                            {
                                "role": "system",
                                "content": "You summarize cryptography papers for a PQC PhD student. Return compact JSON only.",
                            },
                            {
                                "role": "user",
                                "content": (
                                    "Create bilingual summaries for this paper. JSON keys: "
                                    "en_summary, zh_summary, relevance_en, relevance_zh. "
                                    "Each summary should be 2-3 sentences, technical but readable.\n"
                                    + json.dumps(prompt, ensure_ascii=False)
                                ),
                            },
                        ],
                        "text": {"format": {"type": "json_object"}},
                    },
                    api_key,
                )
                content = response.get("output", [{}])[0].get("content", [{}])[0].get("text", "")
            parsed = json.loads(content)
            paper.en_summary = parsed.get("en_summary", "")
            paper.zh_summary = parsed.get("zh_summary", "")
            paper.relevance_en = parsed.get("relevance_en", "")
            paper.relevance_zh = parsed.get("relevance_zh", "")
        except Exception:
            summarize_without_llm(paper)


def collect(config: dict[str, Any]) -> list[Paper]:
    papers: list[Paper] = []
    for name, fetcher in FETCHERS.items():
        if not config["sources"].get(name, {}).get("enabled", False):
            continue
        try:
            papers.extend(fetcher(config))
        except Exception as exc:
            print(f"[warn] {name} failed: {exc}", file=sys.stderr)
    filtered = [p for p in papers if within_lookback(p, config["digest"].get("lookback_days", 2))]
    for paper in filtered:
        classify_and_score(paper, config)
    pqc_labels = {"PQC", "NTT", "polynomial multiplication"}
    relevant = [p for p in filtered if p.score > 0 and (pqc_labels.intersection(p.labels))]
    return sorted(dedupe(relevant), key=lambda p: p.score, reverse=True)[: config["digest"].get("max_papers", 10)]


def load_seen(config: dict[str, Any]) -> set[str]:
    state_path = BASE_DIR / config["digest"].get("state_file", "seen_papers.json")
    if not state_path.exists():
        return set()
    try:
        return set(json.loads(state_path.read_text(encoding="utf-8")).get("seen", []))
    except Exception:
        return set()


def save_seen(config: dict[str, Any], seen: set[str]) -> None:
    state_path = BASE_DIR / config["digest"].get("state_file", "seen_papers.json")
    state_path.write_text(json.dumps({"seen": sorted(seen)[-5000:]}, indent=2), encoding="utf-8")


def render_html(papers: list[Paper], config: dict[str, Any]) -> str:
    today = dt.date.today().isoformat()
    items = []
    for idx, paper in enumerate(papers, 1):
        labels = " ".join(f"<span class='label'>{html.escape(label)}</span>" for label in paper.labels)
        authors = html.escape(", ".join(paper.authors[:8]) + (" et al." if len(paper.authors) > 8 else ""))
        items.append(
            f"""
            <article class="paper">
              <h2>{idx}. <a href="{html.escape(paper.url)}">{html.escape(paper.title)}</a></h2>
              <p class="meta">{html.escape(paper.source)} | {html.escape(paper.published)} | {html.escape(paper.venue)} | score {paper.score}</p>
              <p class="authors">{authors}</p>
              <p>{labels}</p>
              <h3>中文摘要</h3>
              <p>{html.escape(paper.zh_summary)}</p>
              <h3>English Summary</h3>
              <p>{html.escape(paper.en_summary)}</p>
              <h3>相关性 / Relevance</h3>
              <p>{html.escape(paper.relevance_zh)}</p>
              <p>{html.escape(paper.relevance_en)}</p>
            </article>
            """
        )
    body = "\n".join(items) if items else "<p>今天没有发现新的高相关 PQC 文献。</p><p>No highly relevant new PQC papers were found today.</p>"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; line-height: 1.55; color: #202124; }}
    .wrap {{ max-width: 860px; margin: 0 auto; padding: 24px; }}
    h1 {{ font-size: 24px; margin-bottom: 4px; }}
    h2 {{ font-size: 18px; margin-bottom: 4px; }}
    h3 {{ font-size: 14px; margin-bottom: 2px; }}
    a {{ color: #0b57d0; }}
    .paper {{ border-top: 1px solid #d9dce0; padding: 18px 0; }}
    .meta, .authors {{ color: #5f6368; font-size: 13px; }}
    .label {{ display: inline-block; margin: 2px 4px 2px 0; padding: 2px 7px; border: 1px solid #ccd2d8; border-radius: 999px; font-size: 12px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>{html.escape(config["digest"].get("title", "Daily PQC Literature Digest"))}</h1>
    <p class="meta">{today} | top {len(papers)} papers | bilingual digest</p>
    {body}
  </div>
</body>
</html>"""


def render_markdown(papers: list[Paper], config: dict[str, Any]) -> str:
    lines = [f"# {config['digest'].get('title', 'Daily PQC Literature Digest')}", "", f"Date: {dt.date.today().isoformat()}", ""]
    if not papers:
        lines += ["今天没有发现新的高相关 PQC 文献。", "", "No highly relevant new PQC papers were found today."]
    for idx, paper in enumerate(papers, 1):
        lines += [
            f"## {idx}. {paper.title}",
            "",
            f"- Source: {paper.source}",
            f"- Published: {paper.published}",
            f"- Venue: {paper.venue}",
            f"- URL: {paper.url}",
            f"- Labels: {', '.join(paper.labels)}",
            f"- Score: {paper.score}",
            "",
            "### 中文摘要",
            paper.zh_summary,
            "",
            "### English Summary",
            paper.en_summary,
            "",
            "### 相关性 / Relevance",
            paper.relevance_zh,
            "",
            paper.relevance_en,
            "",
        ]
    return "\n".join(lines)


def send_email(html_body: str, markdown_body: str, config: dict[str, Any]) -> None:
    email_cfg = config["email"]
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")
    if not email_cfg.get("enabled") or not username or not password:
        return
    message = email.message.EmailMessage()
    message["From"] = email_cfg["from"]
    message["To"] = ", ".join(email_cfg["to"])
    message["Subject"] = f"{email_cfg.get('subject_prefix', '[PQC Digest]')} {dt.date.today().isoformat()}"
    message.set_content(markdown_body)
    message.add_alternative(html_body, subtype="html")
    context = ssl.create_default_context()
    with smtplib.SMTP(email_cfg["smtp_host"], int(email_cfg.get("smtp_port", 587))) as server:
        server.starttls(context=context)
        try:
            server.login(username, password)
        except smtplib.SMTPAuthenticationError as exc:
            detail = exc.smtp_error.decode("utf-8", errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
            raise RuntimeError(
                "SMTP authentication failed. If you are using Office365/UCC/Outlook and the message says "
                "'basic authentication is disabled', switch to a sender that supports SMTP app passwords "
                "such as Gmail, or implement Microsoft Graph OAuth for Office365 sending. "
                f"Server response: {detail}"
            ) from exc
        server.send_message(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(BASE_DIR / "config.json"))
    parser.add_argument("--dry-run", action="store_true", help="Generate the report without sending email or updating seen state.")
    parser.add_argument("--include-seen", action="store_true", help="Include papers already sent before.")
    args = parser.parse_args()

    load_env(BASE_DIR / ".env")
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Missing config: {config_path}. Copy config.example.json to config.json first.", file=sys.stderr)
        return 2
    config = load_json(config_path)
    papers = collect(config)
    seen = load_seen(config)
    if not args.include_seen:
        papers = [paper for paper in papers if paper.key not in seen]
    summarize_with_llm(papers, config)

    output_dir = BASE_DIR / config["digest"].get("output_dir", "reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = dt.date.today().isoformat()
    html_body = render_html(papers, config)
    markdown_body = render_markdown(papers, config)
    (output_dir / f"{stem}.html").write_text(html_body, encoding="utf-8")
    (output_dir / f"{stem}.md").write_text(markdown_body, encoding="utf-8")

    if not args.dry_run:
        send_email(html_body, markdown_body, config)
        seen.update(paper.key for paper in papers)
        save_seen(config, seen)
    print(f"Generated {len(papers)} papers: {output_dir / f'{stem}.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
