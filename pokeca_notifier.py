#!/usr/bin/env python3
"""ポケカ販売・抽選情報の通知ツール

ポケカの販売・抽選情報をまとめているサイトを1日1回読み、
ポケモンセンターオンラインと Amazon の情報だけを抜き出して
一覧ページ(HTML)にまとめ、Discord Webhook で通知する。

情報源:
- まとめサイトのRSS → 新しい「抽選・予約情報」記事 → 記事内のショップ別の表から
  ポケセン/Amazon の行を抜き出す(記事は追記されるので、一定期間は毎日読み直す)
- Amazon招待リクエスト受付中の商品一覧ページ → 表の商品を拾う

標準ライブラリだけで動く(Python 3.9 以上)。ログイン・応募・購入はしない。
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
JST = dt.timezone(dt.timedelta(hours=9))
USER_AGENT = "Mozilla/5.0 (compatible; pokeca-notifier/2.0; personal daily check)"

SHOPS = {
    "ポケモンセンターオンライン": ["ポケモンセンターオンライン", "ポケセンオンライン", "ポケモンセンター", "ポケセン"],
    "Amazon": ["Amazon", "amazon", "AMAZON", "アマゾン"],
}

DEFAULT_CONFIG = {
    "discord_webhook_url": "",
    "output_html": "docs/index.html",
    "state_file": "state.json",
    "request_interval_sec": 5,
    "recheck_days": 45,
    "feeds": [
        {"id": "gamepedia", "name": "攻略大百科", "url": "https://premium.gamepedia.jp/pokeca/feed",
         "title_keywords": ["抽選", "予約"]},
        {"id": "zanmai", "name": "ポケざんまい", "url": "https://www.pokeca-zanmai.jp/feed",
         "title_keywords": ["抽選", "予約"]},
    ],
    "pages": [
        {"id": "zanmai_amazon", "name": "ポケざんまい", "url": "https://www.pokeca-zanmai.jp/amazon-lottery/"},
    ],
}

DATE_RE = re.compile(r"(?:(20\d{2})[年/.\-])?(\d{1,2})[月/](\d{1,2})日?")


def now_jst() -> dt.datetime:
    return dt.datetime.now(JST)


def load_config(path: Path) -> dict:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if path.exists():
        config.update(json.loads(path.read_text(encoding="utf-8")))
    # GitHub Actions では Secrets から環境変数で渡す
    config["discord_webhook_url"] = os.environ.get("DISCORD_WEBHOOK_URL") or config["discord_webhook_url"]
    return config


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def match_shop(text: str) -> str | None:
    for shop, words in SHOPS.items():
        if any(w in text for w in words):
            return shop
    return None


class BlockParser(HTMLParser):
    """本文を見出し(h2〜h4)と表の並びに変換する。表のセルはテキストとリンクを持つ。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self._heading: dict | None = None
        self._tables: list[dict] = []
        self._cell: dict | None = None
        self._skip = 0
        self.texts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag in ("h2", "h3", "h4") and not self._tables:
            self._heading = {"type": "h", "level": int(tag[1]), "text": ""}
        elif tag == "table":
            self._tables.append({"type": "table", "rows": []})
        elif tag == "tr" and self._tables:
            self._tables[-1]["rows"].append([])
        elif tag in ("td", "th") and self._tables:
            if not self._tables[-1]["rows"]:
                self._tables[-1]["rows"].append([])
            self._cell = {"text": "", "links": []}
            self._tables[-1]["rows"][-1].append(self._cell)
        elif tag == "a" and self._cell is not None:
            href = dict(attrs).get("href")
            if href:
                self._cell["links"].append(href)
        elif tag == "br" and self._cell is not None:
            self._cell["text"] += " "

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("h2", "h3", "h4") and self._heading is not None:
            self._heading["text"] = clean(self._heading["text"])
            self.blocks.append(self._heading)
            self._heading = None
        elif tag == "table" and self._tables:
            table = self._tables.pop()
            for row in table["rows"]:
                for cell in row:
                    cell["text"] = clean(cell["text"])
            table["rows"] = [r for r in table["rows"] if r]
            if not self._tables:
                self.blocks.append(table)
        elif tag in ("td", "th"):
            self._cell = None

    def handle_data(self, data):
        if self._skip:
            return
        self.texts.append(data)
        if self._cell is not None:
            self._cell["text"] += data
        elif self._heading is not None:
            self._heading["text"] += data


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.5"})
    with urllib.request.urlopen(req, timeout=30) as res:
        charset = res.headers.get_content_charset() or "utf-8"
        return res.read().decode(charset, errors="replace")


def parse_feed(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text.strip().encode("utf-8"))
    entries = []
    for item in root.iter("item"):  # RSS 2.0
        entries.append({"title": clean(item.findtext("title") or ""), "url": (item.findtext("link") or "").strip()})
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):  # Atom
        link = entry.find("a:link", ns)
        entries.append({"title": clean(entry.findtext("a:title", "", ns)),
                        "url": link.get("href", "") if link is not None else ""})
    return [e for e in entries if e["url"]]


def product_name(article_title: str) -> str:
    name = re.sub(r"【[^】]*】", "", article_title)
    name = re.split(r"の(?:収録|当たり|抽選|予約|最新|発売)|抽選・予約|予約・抽選|\s*[|｜]", name)[0]
    return clean(name) or clean(article_title)


RELEASE_RE = re.compile(r"発売日[^0-9]{0,12}((?:20\d{2}年)?\d{1,2}月\d{1,2}日(?:\s*[(（][^)）]*[)）])?)")


def find_release(text: str) -> str:
    m = RELEASE_RE.search(text)
    return clean(m.group(1)) if m else ""


def name_key(name: str) -> str:
    """商品名の表記ゆれをそろえる(「」・空白・パック種別を除く)。"""
    name = re.sub(r"拡張パック|強化拡張パック|ハイクラスパック|ポケモンカードゲーム|MEGA\s|[「」『』()（）\s・]", "", name)
    return name.lower()


def first_date(text: str) -> str:
    m = DATE_RE.search(text)
    return f"{int(m.group(2))}/{int(m.group(3))}" if m else ""


def pick_link(links: list[str], base: str, shop: str) -> str:
    domains = ("pokemoncenter-online.com",) if shop == "ポケモンセンターオンライン" else ("amazon.", "amzn.")
    for href in links:
        url = urllib.parse.urljoin(base, href)
        parts = urllib.parse.urlparse(url)
        if any(d in parts.netloc for d in domains):
            # まとめサイトのアフィリエイト用パラメータなどを外す
            return f"{parts.scheme}://{parts.netloc}{parts.path}" if "amazon." in parts.netloc else url
    return ""


def table_to_lines(rows: list[list[dict]]) -> list[str]:
    lines = []
    for row in rows:
        texts = [c["text"] for c in row if c["text"]]
        if len(texts) >= 2:
            lines.append(f"{texts[0]}: {' / '.join(texts[1:])}")
        elif texts:
            lines.append(texts[0])
    return lines


def guess_kind(text: str) -> str:
    if "招待" in text:
        return "招待リクエスト"
    if "抽選" in text:
        return "抽選"
    if "予約" in text:
        return "予約"
    return "販売"


def extract_from_article(article: dict, page_html: str, source_name: str) -> list[dict]:
    """記事の表から ポケセン/Amazon の情報を取り出す。

    1) 直前の見出しがショップ名の表(ショップ別の詳細表)を優先し、
    2) それがないショップは、1列目がショップ名の行(まとめ表の行)を使う。
    """
    parser = BlockParser()
    parser.feed(page_html)
    product = product_name(article["title"])
    release = find_release(" ".join(parser.texts))
    detail: dict[str, dict] = {}
    summary: dict[str, dict] = {}
    heading_shop = None
    for block in parser.blocks:
        if block["type"] == "h":
            heading_shop = match_shop(block["text"])
            continue
        rows = block["rows"]
        if heading_shop and heading_shop not in detail:
            lines = table_to_lines(rows)
            links = [href for row in rows for c in row for href in c["links"]]
            product_here = product
            for line in lines:
                if line.startswith(("対象商品", "商品名")) and ": " in line:
                    product_here = line.split(": ", 1)[1]
                    break
            detail[heading_shop] = {"product": product_here, "lines": lines, "text": " ".join(lines),
                                    "link": pick_link(links, article["url"], heading_shop)}
            continue
        for row in rows:
            if not row or not row[0]["text"]:
                continue
            shop = match_shop(row[0]["text"])
            if shop and shop not in summary:
                lines = [c["text"] for c in row[1:] if c["text"]]
                links = [href for c in row for href in c["links"]]
                summary[shop] = {"product": product, "lines": lines, "text": " ".join(lines),
                                 "link": pick_link(links, article["url"], shop)}

    items = []
    for shop in SHOPS:
        info = detail.get(shop) or summary.get(shop)
        if not info or not info["text"]:
            continue
        start = ""
        for line in info["lines"]:
            if re.match(r"(抽選|予約|応募|受付)(開始|期間)", line):
                start = first_date(line)
                break
        # 同じ記事・同じショップでも、受付開始日が変われば(再販・2次抽選など)新着として扱う。
        # 日付がない場合(Amazonの招待リクエストなど)は、記事とショップの組で1件とする
        start = start or first_date(info["text"])
        items.append({
            "key": f"{article['url']}|{shop}|{start}",
            "shop": shop,
            "product": info["product"],
            "kind": guess_kind(info["text"]),
            "details": info["lines"][:8],
            "link": info["link"],
            "release": release,
            "source_url": article["url"],
            "source_name": source_name,
        })
    return items


def extract_amazon_list(page: dict, page_html: str) -> list[dict]:
    """Amazon招待リクエスト一覧ページの表から、Amazonリンクの付いた商品を拾う。"""
    parser = BlockParser()
    parser.feed(page_html)
    items = []
    history: dict[str, dict] = {}  # 実績表: 商品名 → 発売日・受付開始
    for block in parser.blocks:
        if block["type"] != "table":
            continue
        for row in block["rows"]:
            texts = [c["text"] for c in row]
            if len(texts) >= 3 and DATE_RE.search(texts[1]) and DATE_RE.search(texts[2]):
                history[name_key(texts[0])] = {"release": texts[1], "start": texts[2]}
    for block in parser.blocks:
        if block["type"] != "table":
            continue
        for row in block["rows"]:
            links = [href for c in row for href in c["links"]]
            link = pick_link(links, page["url"], "Amazon")
            name = row[0]["text"] if row else ""
            if not link or not name:
                continue
            past = next((v for k, v in history.items() if k and (k in name_key(name) or name_key(name) in k)), {})
            items.append({
                "key": f"amazon|{link.split('?')[0]}",
                "shop": "Amazon",
                "product": name,
                "kind": "招待リクエスト",
                "details": ["招待リクエスト受付中(まとめサイト掲載)"]
                           + ([f"受付開始: {past['start']}"] if past.get("start") else []),
                "link": link,
                "release": past.get("release", ""),
                "source_url": page["url"],
                "source_name": page["name"],
            })
    return items


def load_state(path: Path) -> dict:
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if "articles" in state:
            return state
    return {"items": {}, "articles": {}, "runs": []}


def save_state(path: Path, state: dict) -> None:
    state["runs"] = state["runs"][-30:]
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def post_discord(webhook_url: str, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(webhook_url, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": USER_AGENT})
    for attempt in range(3):
        try:
            urllib.request.urlopen(req, timeout=30).close()
            return
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(float(e.headers.get("Retry-After", "2")))
                continue
            raise


SHOP_LABEL = {"ポケモンセンターオンライン": "ポケモンセンターオンライン", "Amazon": "Amazon"}
SHOP_ICON = {"ポケモンセンターオンライン": "🔴", "Amazon": "🟠"}
DEFAULT_APPLY_URL = {
    "ポケモンセンターオンライン": "https://www.pokemoncenter-online.com/lottery/apply.html",
    "Amazon": "",
}
DATETIME_RE = re.compile(r"(\d{1,2})/(\d{1,2})(?:\([^)]*\))?\s*(\d{1,2}):(\d{2})?")


def details_map(item: dict) -> dict[str, str]:
    result = {}
    for line in item["details"]:
        if ": " in line:
            key, value = line.split(": ", 1)
            result.setdefault(key, value)
    return result


def find_value(d: dict[str, str], *words: str) -> str:
    for key, value in d.items():
        if any(w in key for w in words):
            return value
    return ""


def to_datetime(text: str, now: dt.datetime, end_of_day: bool) -> dt.datetime | None:
    m = DATETIME_RE.search(text)
    if m:
        month, day, hour, minute = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0)
    else:
        m = DATE_RE.search(text)
        if not m:
            return None
        month, day = int(m.group(2)), int(m.group(3))
        hour, minute = (23, 59) if end_of_day else (0, 0)
    year = now.year + (1 if month < now.month - 6 else 0) - (1 if month > now.month + 6 else 0)
    try:
        return dt.datetime(year, month, day, hour, minute, tzinfo=JST)
    except ValueError:
        return None


def summarize(item: dict, now: dt.datetime) -> dict:
    """通知・一覧で使う「いつ・何をすればいいか」をまとめる。"""
    d = details_map(item)
    start = find_value(d, "抽選開始", "予約開始", "受付開始", "応募開始")
    end = find_value(d, "抽選終了", "予約終了", "受付終了", "応募終了")
    period = f"{start} 〜 {end}" if start or end else find_value(d, "抽選期間", "予約期間", "受付期間", "応募期間")
    if not period and item["details"] and ": " not in item["details"][0] and "まとめサイト掲載" not in item["details"][0]:
        period = " / ".join(item["details"])  # まとめ表の行(列名なし)はそのまま出す
    start_dt = to_datetime(start or period, now, False)
    end_dt = to_datetime(end or (period.split("〜")[-1] if "〜" in period else ""), now, True)
    invite = item["kind"] == "招待リクエスト" and item["shop"] == "Amazon"
    if invite and not end:
        status = "受付中"
    elif end_dt and now > end_dt:
        status = "受付終了"
    elif start_dt and now < start_dt:
        status = "受付前"
    elif start_dt or end_dt:
        status = "受付中"
    else:
        status = "詳細を確認"
    release = item.get("release", "")
    if invite and status == "受付中":
        deadline = "(締切なし・予告なく終了" + (f"・発売日 {release}" if release else "") + ")"
        period = f"{start} 〜 締切なし(予告なく終了することがあります)" if start else "締切なし(予告なく終了することがあります)"
    else:
        deadline = f"({end}まで)" if status == "受付中" and end else f"({start}から)" if status == "受付前" and start else ""
    kind = item["kind"] if item["kind"] != "販売" else "販売"
    return {
        "status": status,
        "headline": f"{SHOP_LABEL[item['shop']]}で{kind}{status}{deadline}",
        "period": period,
        "result": find_value(d, "結果発表", "当選発表"),
        "purchase": find_value(d, "購入期間", "支払") or ("当選メールが届いてから48〜72時間ほど(メールに記載)" if invite else ""),
        "release": release,
        "seen": f"{item['first_seen'][:10].replace('-', '/')}(このツールで初めて確認した日)" if invite and not start and item.get("first_seen") else "",
        "delivery": find_value(d, "お届け", "発送"),
        "limit": find_value(d, "購入制限", "制限").lstrip("・"),
        "apply_url": item["link"] or DEFAULT_APPLY_URL[item["shop"]] or item["source_url"],
        "how": ("Amazonの商品ページで「招待をリクエスト」を押す(当選するとメールで購入案内)"
                if item["kind"] == "招待リクエスト" else
                "ポケモンセンターオンラインの抽選ページから応募(ログインが必要)"
                if item["shop"] == "ポケモンセンターオンライン" and item["kind"] == "抽選" else ""),
    }


def notify(webhook_url: str, new_items: list[dict], errors: list[str], first_run: bool) -> None:
    if not webhook_url:
        print("Discord Webhook URL が未設定のため通知をスキップしました")
        return
    if first_run:
        post_discord(webhook_url, {"content": f"ポケカ通知ツールを開始しました。現在の {len(new_items)} 件を登録しました。次回から新着だけ通知します。"})
    else:
        now = now_jst()
        for i in range(0, len(new_items), 10):
            chunk = new_items[i:i + 10]
            embeds, lines = [], []
            for item in chunk:
                info = summarize(item, now)
                icon = SHOP_ICON[item["shop"]]
                lines.append(f"{icon} **{item['product']}** … {info['headline']}")
                fields = [{"name": name, "value": value[:1024], "inline": inline} for name, value, inline in (
                    ("ショップ", SHOP_LABEL[item["shop"]], True),
                    ("種類", item["kind"], True),
                    ("状況", info["status"], True),
                    ("応募期間", info["period"], False),
                    ("応募方法", info["how"], False),
                    ("当選発表", info["result"], False),
                    ("購入期間", info["purchase"], False),
                    ("掲載確認", info["seen"], False),
                    ("発売日", info["release"], False),
                    ("お届け", info["delivery"], False),
                    ("購入制限", info["limit"], False),
                ) if value]
                fields.append({"name": "リンク", "value": f"[応募・商品ページ]({info['apply_url']}) ・ [まとめ記事]({item['source_url']})", "inline": False})
                embeds.append({
                    "author": {"name": f"{icon} {SHOP_LABEL[item['shop']]}"},
                    "title": f"{item['product']}"[:256],
                    "url": info["apply_url"],
                    "description": f"**{info['headline']}**",
                    "fields": fields,
                    "color": 0xE3350D if item["shop"] != "Amazon" else 0xFF9900,
                    "footer": {"text": f"出典: {item['source_name']}"},
                })
            header = "📢 ポケカの新着情報" if i == 0 else ""
            payload = {"content": "\n".join(filter(None, [header, *lines]))[:2000], "embeds": embeds}
            post_discord(webhook_url, payload)
    if errors:
        post_discord(webhook_url, {"content": "取得に失敗したページがあります:\n" + "\n".join(errors)[:1800]})


def render_html(state: dict, path: Path) -> None:
    e = html.escape
    items = sorted(state["items"].values(), key=lambda x: x["first_seen"], reverse=True)
    last_run = state["runs"][-1] if state["runs"] else {}
    rows = []
    now = now_jst()
    for item in items:
        info = summarize(item, now)
        target = info["apply_url"]
        details = "".join(f"<li>{e(d)}</li>" for d in item["details"])
        rows.append(
            f'<li class="item {"amazon" if item["shop"] == "Amazon" else "pco"}">'
            f'<span class="tag">{e(item["shop"])}</span><span class="kind">{e(item["kind"])}</span>'
            f'<a class="title" href="{e(target)}" target="_blank" rel="noopener">{e(item["product"])}</a>'
            f'<div class="headline">{e(info["headline"])}</div>'
            f'<ul class="details">{details}</ul>'
            f'<div class="meta">初回確認 {e(item["first_seen"][:10])} ・ 出典 '
            f'<a href="{e(item["source_url"])}" target="_blank" rel="noopener">{e(item["source_name"])}</a></div></li>'
        )
    errors = "".join(f"<li>{e(x)}</li>" for x in last_run.get("errors", []))
    page = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ポケカ販売・抽選情報</title>
<style>
:root {{ --bg:#f7f7f9; --card:#fff; --text:#1d1d1f; --muted:#6b6b76; --line:#e3e3e8; --pco:#e3350d; --amazon:#c77700; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141418; --card:#1e1e24; --text:#ececf1; --muted:#9a9aa6; --line:#33333b; --pco:#ff6a45; --amazon:#ffb340; }} }}
body {{ margin:0; background:var(--bg); color:var(--text); font-family:system-ui,-apple-system,"Hiragino Sans","Yu Gothic UI",sans-serif; }}
main {{ max-width:860px; margin:0 auto; padding:24px 16px; }}
h1 {{ font-size:1.4rem; margin:0 0 4px; }}
.sub {{ color:var(--muted); font-size:.85rem; margin-bottom:20px; }}
ul {{ list-style:none; padding:0; margin:0; }}
.item {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; margin-bottom:10px; overflow-wrap:anywhere; }}
.title {{ display:block; color:var(--text); font-weight:600; text-decoration:none; margin-top:6px; }}
.title:hover {{ text-decoration:underline; }}
.tag {{ display:inline-block; font-size:.75rem; color:#fff; border-radius:4px; padding:1px 6px; margin-right:6px; }}
.pco .tag {{ background:var(--pco); }} .amazon .tag {{ background:var(--amazon); }}
.kind {{ font-size:.75rem; color:var(--muted); }}
.headline {{ font-weight:600; font-size:.9rem; margin-top:4px; }}
.details {{ font-size:.85rem; margin:6px 0 0; }} .details li {{ padding:1px 0; }}
.meta {{ color:var(--muted); font-size:.8rem; margin-top:6px; }} .meta a {{ color:inherit; }}
.errors {{ color:var(--pco); font-size:.85rem; margin-bottom:12px; }}
</style></head><body><main>
<h1>ポケカ販売・抽選情報(ポケセン・Amazon)</h1>
<div class="sub">まとめサイトから毎日取得 ・ 最終更新 {e(last_run.get("at", "-")[:16].replace("T", " "))} ・ {len(items)} 件</div>
{f'<ul class="errors">{errors}</ul>' if errors else ""}
<ul>{"".join(rows) or "<li class='item'>まだ情報がありません</li>"}</ul>
</main></body></html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")


class Fetcher:
    """リクエストの間隔を空ける。--offline-dir 指定時は保存済みファイルを読む。"""

    def __init__(self, interval: float, offline_dir: Path | None, dump_dir: Path | None):
        self.interval, self.offline_dir, self.dump_dir = interval, offline_dir, dump_dir
        self._last = 0.0

    @staticmethod
    def file_name(url: str) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "_", url)[:150] + ".html"

    def get(self, url: str) -> str:
        if self.offline_dir:
            return (self.offline_dir / self.file_name(url)).read_text(encoding="utf-8")
        wait = self.interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            text = fetch(url)
        finally:
            self._last = time.monotonic()
        if self.dump_dir:
            self.dump_dir.mkdir(exist_ok=True)
            (self.dump_dir / self.file_name(url)).write_text(text, encoding="utf-8")
        return text


def main() -> int:
    ap = argparse.ArgumentParser(description="ポケカ販売・抽選情報の通知ツール")
    ap.add_argument("--config", default=str(BASE_DIR / "config.json"))
    ap.add_argument("--no-notify", action="store_true", help="Discord に送らない")
    ap.add_argument("--dump-html", action="store_true", help="取得したページを debug/ に保存する(調整用)")
    ap.add_argument("--test-notify", action="store_true", help="最新の1件をテストとして Discord に送るだけ")
    ap.add_argument("--offline-dir", help="ネットに接続せず、このフォルダの保存済みページを使う(テスト用)")
    args = ap.parse_args()

    config = load_config(Path(args.config))
    state_path = BASE_DIR / config["state_file"]
    state = load_state(state_path)
    first_run = not state["items"] and not state["runs"]
    if args.test_notify:
        newest = sorted(state["items"].values(), key=lambda x: x["first_seen"], reverse=True)
        latest = [next(x for x in newest if x["shop"] == shop) for shop in SHOPS if any(x["shop"] == shop for x in newest)]
        if not config["discord_webhook_url"]:
            print("DISCORD_WEBHOOK_URL が設定されていません", file=sys.stderr)
            return 1
        post_discord(config["discord_webhook_url"], {"content": "【テスト】ポケカ通知ツールの通知テストです。毎朝8時に新着をお知らせします。"})
        notify(config["discord_webhook_url"], latest, [], False)
        return 0
    fetcher = Fetcher(config["request_interval_sec"],
                      Path(args.offline_dir) if args.offline_dir else None,
                      BASE_DIR / "debug" if args.dump_html else None)
    now = now_jst()
    started = now.isoformat(timespec="seconds")
    found, errors = [], []

    # 1) RSS から抽選・予約の記事を見つけて記録する
    for feed in config["feeds"]:
        try:
            entries = parse_feed(fetcher.get(feed["url"]))
            for entry in entries:
                if any(k in entry["title"] for k in feed["title_keywords"]) and entry["url"] not in state["articles"]:
                    state["articles"][entry["url"]] = {**entry, "source_name": feed["name"], "first_seen": started}
            print(f"{feed['name']} RSS: {len(entries)} 件")
        except Exception as ex:  # 1つ失敗しても他は続ける
            errors.append(f"{feed['name']} RSS: {type(ex).__name__} {ex}")

    # 2) 最近の記事を読み直して ポケセン/Amazon の情報を抜き出す(記事は後から追記されるため)
    cutoff = now - dt.timedelta(days=config["recheck_days"])
    page_urls = {page["url"] for page in config["pages"]}  # 一覧ページは 3) で別に読む
    for url, article in list(state["articles"].items()):
        if url in page_urls or dt.datetime.fromisoformat(article["first_seen"]) < cutoff:
            continue
        try:
            items = extract_from_article(article, fetcher.get(url), article["source_name"])
            print(f"記事 {article['title'][:40]}: {len(items)} 件")
            found.extend(items)
        except Exception as ex:
            errors.append(f"{article['title'][:40]}: {type(ex).__name__} {ex}")

    # 3) 一覧ページ(Amazon招待リクエストなど)
    for page in config["pages"]:
        try:
            items = extract_amazon_list(page, fetcher.get(page["url"]))
            print(f"{page['name']} 一覧: {len(items)} 件")
            found.extend(items)
        except Exception as ex:
            errors.append(f"{page['name']}: {type(ex).__name__} {ex}")

    # 発売日が分からない商品は、同じ商品名の記事から補う
    releases = {name_key(i["product"]): i["release"] for i in found if i.get("release")}
    releases.update({name_key(a.get("product", "")): a["release"] for a in state["articles"].values() if a.get("release")})
    for item in found:
        if not item.get("release"):
            key = name_key(item["product"])
            item["release"] = next((r for k, r in releases.items() if k and (k in key or key in k)), "")

    new_items = []
    for item in found:
        old = state["items"].get(item["key"])
        if old:
            old.update({k: item[k] for k in ("details", "link", "kind", "product", "release")})  # 最新の内容に更新
            continue
        item["first_seen"] = started
        state["items"][item["key"]] = item
        new_items.append(item)

    for err in errors:
        print("取得失敗:", err, file=sys.stderr)
    state["runs"].append({"at": started, "found": len(found), "new": len(new_items), "errors": errors})
    save_state(state_path, state)
    render_html(state, BASE_DIR / config["output_html"])
    print(f"新着 {len(new_items)} 件 / エラー {len(errors)} 件")

    if not args.no_notify and (new_items or errors):
        notify(config["discord_webhook_url"], new_items, errors, first_run)
    return 1 if errors and not found else 0


if __name__ == "__main__":
    sys.exit(main())
