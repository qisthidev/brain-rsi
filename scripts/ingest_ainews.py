#!/usr/bin/env python3
"""Ingest RSS feed from https://ainews.imrenagi.com and generate structured synthesis."""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

FEED_URL = "https://ainews.imrenagi.com/rss.xml"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "wiki" / "research"


def fetch_feed(url: str = FEED_URL) -> list[dict]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "brain-rsi-feed-ingest/1.0 (+https://github.com/qisthidev/brain-rsi)"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        content = resp.read()

    root = ET.fromstring(content)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("Invalid RSS feed: <channel> element not found")

    items = []
    for item_el in channel.findall("item"):
        title = (item_el.findtext("title") or "").strip()
        link = (item_el.findtext("link") or "").strip()
        pub_date = (item_el.findtext("pubDate") or "").strip()
        description = (item_el.findtext("description") or "").strip()

        categories = [cat.text.strip() for cat in item_el.findall("category") if cat.text]

        items.append({
            "title": title,
            "link": link,
            "pub_date": pub_date,
            "description": description,
            "categories": categories,
        })
    return items


def categorize_item(item: dict) -> str:
    title_lower = item["title"].lower()
    cats = [c.lower() for c in item["categories"]]
    all_text = f"{title_lower} {' '.join(cats)}"

    if "daily-digest" in cats or "daily digest" in title_lower:
        return "Daily Digest"
    if any(k in all_text for k in ["agent", "harness", "slack code", "workflow", "multiplayer"]):
        return "Agent Architecture & Harness"
    if any(k in all_text for k in ["debugging", "safety", "security", "injection", "permission", "watermark", "audit"]):
        return "AI Safety, Security & Debugging"
    if any(k in all_text for k in ["model", "vision", "grok", "deepseek", "qwen", "routing", "openrouter", "compiler", "mojo"]):
        return "Model Releases, Routing & Infrastructure"
    if any(k in all_text for k in ["math", "exponent", "matrix", "vectors", "embeddings", "optimization"]):
        return "Math & Theory for AI"
    return "AI Applications & Research"


def generate_markdown(items: list[dict], generated_date: str) -> str:
    categories_map: dict[str, list[dict]] = {}
    for it in items:
        cat = categorize_item(it)
        categories_map.setdefault(cat, []).append(it)

    md_lines = [
        "---",
        f"title: Ingest & Sintesis ainews.imrenagi.com ({generated_date})",
        "type: external-signal-digest",
        "status: active",
        f"created: {generated_date}",
        "sources:",
        f"  - {FEED_URL}",
        "related:",
        "  - wiki/research/ai-scientist-v2-untuk-brain-v2.md",
        "  - CLAUDE.md",
        "  - eval/cases.json",
        "---",
        "",
        f"# Ingest Signal: What's Viral in AI (ainews.imrenagi.com) — {generated_date}",
        "",
        "> **Sumber Publikasi**: [What's Viral in AI](https://ainews.imrenagi.com) oleh Imre Nagi.  ",
        f"> **Feed Terproses**: `{FEED_URL}` | **Total Artikel**: {len(items)} artikel  ",
        f"> **Fokus RSI brain-v2.1**: Pola arsitektur agent, permission gates, failure-first protocols, debugging workflows, dan optimasi latency/cost per task.",
        "",
        "## 1. Highlight Sinyal Utama untuk RSI (Recursive Self-Improvement)",
        "",
    ]

    # Detailed highlight synthesis for top actionable items
    highlights = [
        ("Linus",
         "Pola *evidence ledger*, *reversible probes*, dan *human stop gate*. Saat agent debugging, jangan loop blindly; catat setiap probe secara terisolasi dan sediakan titik henti deterministik."),
        ("Slack Code",
         "Agent collaboration memerlukan strict isolation boundary. Menguatkan prinsip `CLAUDE.md`: agent hanya mengusulkan perubahan (proposal/patch), merge gate tetap membutuhkan manusia/CI."),
        ("DeepSeek V4 Flash Vision",
         "Desain workflow multimodal harus mengasumsikan kegagalan terlebih dahulu (*failure-first*): pasang acceptance gates, cost tracking, dan fallback transparan ke text/tool alternatif."),
        ("Stripe",
         "Pentingnya model routing yang netral dan terukur. Relevan untuk routing `ccx` / runner multi-model di mana evaluasi latency dan cost per task harus diverifikasi secara independen."),
        ("Grok 4.6",
         "Menghitung biaya dan kecepatan nyata bukan dari raw token rate, melainkan *cost per accepted task* (termasuk retries & review loops)."),
    ]

    for kw, note in highlights:
        matched = next((it for it in items if kw.lower() in it["title"].lower()), None)
        if matched:
            md_lines.append(f"### [{matched['title']}]({matched['link']})")
            md_lines.append(f"- **Waktu Publikasi**: {matched['pub_date']}")
            md_lines.append(f"- **Kategori**: `{', '.join(matched['categories'])}`")
            md_lines.append(f"- **Intisari Artikel**: {matched['description']}")
            md_lines.append(f"- **Relevansi & Aksi untuk `brain-v2.1`**: {note}")
            md_lines.append("")

    md_lines.extend([
        "## 2. Rincian Feed Berdasarkan Kategori",
        "",
    ])

    for cat_name, cat_items in categories_map.items():
        md_lines.append(f"### {cat_name} ({len(cat_items)} item)")
        for it in cat_items:
            md_lines.append(f"- **[{it['title']}]({it['link']})**")
            md_lines.append(f"  - *Tanggal*: {it['pub_date']}")
            if it['categories']:
                md_lines.append(f"  - *Tags*: `{', '.join(it['categories'])}`")
            if it['description']:
                md_lines.append(f"  - *Summary*: {it['description']}")
        md_lines.append("")

    md_lines.extend([
        "## 3. Rencana Penerapan ke Harness `brain-v2.1`",
        "",
        "| Komponen Brain-v2.1 | Pelajaran yang Diadopsi | Target File |",
        "|---|---|---|",
        "| **Safety & Merge Gates** | Multi-layer permission & human stop gate | `CLAUDE.md`, `src/brain_rsi/cycle.py` |",
        "| **Eval Benchmarking** | Uji skenario failure-first & token cost per task | `eval/cases.json`, `src/brain_rsi/benchmark.py` |",
        "| **Agent Workflows** | Evidence ledger & reversible probe pattern | `agent/PROMPT.md`, `.claude/skills/` |",
        "| **Feed Intake Automation** | Scheduled daily ingest ke knowledge base | `scripts/ingest_ainews.py` |",
        "",
    ])

    return "\n".join(md_lines)


def main():
    parser = argparse.ArgumentParser(description="Ingest ainews.imrenagi.com feed into brain-v2.1")
    parser.add_argument("--output", "-o", type=Path, default=None, help="Output markdown file path")
    parser.add_argument("--json", action="store_true", help="Print raw JSON feed items to stdout")
    parser.add_argument("--limit", "-n", type=int, default=None, help="Limit number of items")
    args = parser.parse_args()

    try:
        items = fetch_feed()
    except Exception as e:
        print(f"Error fetching feed from {FEED_URL}: {e}", file=sys.stderr)
        sys.exit(1)

    if args.limit:
        items = items[:args.limit]

    if args.json:
        print(json.dumps(items, indent=2))
        return

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output_path = args.output or (DEFAULT_OUTPUT_DIR / f"{today_str}-ainews-imrenagi-ingest.md")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    md_content = generate_markdown(items, today_str)
    output_path.write_text(md_content, encoding="utf-8")
    print(f"Ingest sukses! {len(items)} item ditulis ke: {output_path}")


if __name__ == "__main__":
    main()
