#!/usr/bin/env python3
"""每日 GitHub 悬赏摘要（只读，零服务器成本）。

设计目标
- 只读：只调 GitHub 公开搜索接口，不发评论、不提 PR、不投标。
- 便宜：5 个查询 + 少量 core 请求，单次运行约 60-90 秒。
- 不误报：金额分级 + 资助申请门 + 聚合器噪声过滤（三条都是被真实误报打出来的）。
- 可去重：已推送过的 issue 记在 state/seen.json，由 workflow 提交回仓库。

环境变量
- GITHUB_TOKEN   必填。Actions 里用 ${{ secrets.GITHUB_TOKEN }}。
- FEISHU_WEBHOOK 可选。不填就只写文件，不推送。
- WINDOW_HOURS   可选，默认 24。
- MIN_AMOUNT     可选，默认 20（美元）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
FEISHU = os.environ.get("FEISHU_WEBHOOK", "").strip()
WINDOW_HOURS = int(os.environ.get("WINDOW_HOURS", "24"))
MIN_AMOUNT = float(os.environ.get("MIN_AMOUNT", "20"))

STATE_PATH = os.path.join("state", "seen.json")
OUT_DIR = "output"

QUERIES = [
    'label:"💎 Bounty" state:open is:issue',
    'label:"💰 Bounty" state:open is:issue',
    "label:bounty state:open is:issue",
    '"bounty: $" state:open is:issue',
    "USDC bounty state:open is:issue",
]

# 其他 AI agent 发的「赏金告警贴」，本身不是任务
AGGREGATOR_TITLE = re.compile(
    r"bounty\s*(alert|scout|radar|digest|roundup|report|list|hunt|claim)"
    r"|new opportunit|micro\s*bounty|\[radar\]|\[pentest\]"
    r"|赏金[：:]\s*有|赏金快报|悬赏快报"
    r"|bounty\s*candidates?|candidate\s*packet|opportunit(y|ies)\s*found",
    re.I,
)
AGGREGATOR_REPO = re.compile(
    r"bountyscout|bounty[-_]?hunters|bounty[-_]?plaza|bounty[-_]?scout"
    r"|bountybot|bounty[-_]?alert|bounty[-_]?hunter|bounty[-_]?concierge"
    r"|agent[-_]?bounties",
    re.I,
)

# 资助申请（不是悬赏）。2026-10-15 真实误报：ZcashCommunityGrants#493 被判 $600。
GRANT_TITLE = re.compile(
    r"(^|\b)(grant|funding)\s+(application|proposal|request)\b"
    r"|application\s+for\s+(a\s+)?grant|proposal\s+for\s+(a\s+)?grant",
    re.I,
)
GRANT_BODY = re.compile(
    r"Requested Grant Amount|Total Budget \(USD\)|Startup Funding \(USD\)"
    r"|Milestone Details|Grant Agreement|KYC information if funded"
    r"|Compensation Costs \(USD\)",
    re.I,
)
GRANT_EXTERNAL_PAYOUT = re.compile(
    r"algora\.io|merit\.systems|opire\.dev|bountyhub\.dev|superteam(\.fun)?"
    r"|bountysource|issuehunt|gitcoin|polar\.sh|thanks\.dev"
    r"|(?:we|i|the team|maintainers?)\s+(?:will\s+)?pay\b[^.\n]{0,40}\$\s?\d"
    r"|\$\s?\d[\d,]*(?:\.\d+)?\s+(?:for|upon|on)\s+(?:a\s+)?(?:merged?|completed|accepted)\s+(?:pr|pull request|issue|task)",
    re.I,
)

PAYOUT_PLATFORM = re.compile(
    r"algora\.io|merit\.systems|opire\.dev|bountyhub\.dev|superteam(\.fun)?"
    r"|bountysource|issuehunt|gitcoin|polar\.sh|thanks\.dev",
    re.I,
)

FICTIONAL = re.compile(r"\bin[- ]game\b|\bgame money\b|money slot|\bgameplay\b|\bNPC\b", re.I)

RANK = {"strong": 3, "medium": 2, "weak": 1}


def log(msg: str) -> None:
    print(msg, flush=True)


def http_json(url: str, headers: dict | None = None, method: str = "GET", payload: dict | None = None):
    """GET/POST JSON。网络抖动重试 2 次（含 IncompleteRead），HTTP 错误直接返回。"""
    last = None
    for attempt in range(3):
        req = urllib.request.Request(url, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "auto-task")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, data=data, timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            log(f"  ! HTTP {e.code} {url[:90]}")
            return {"__err": e.code}
        except Exception as e:  # IncompleteRead / 超时 / 连接重置
            last = e
            if attempt < 2:
                log(f"  · 重试 {attempt + 1}/2（{type(e).__name__}）")
                time.sleep(2)
    log(f"  ! {type(last).__name__} {url[:90]}")
    return {"__err": str(last)}


def extract_amount(text: str):
    """返回 (金额, 证据等级)。取最小值（宁可低估）。"""
    b = text or ""
    best, tier = None, None

    def push(v, t):
        nonlocal best, tier
        if v is None or v <= 0:
            return
        if best is None or v < best or (v == best and RANK[t] > RANK[tier]):
            best, tier = v, t

    def num(s, k):
        return float(s.replace(",", "")) * (1000 if k else 1)

    # ① 赏金词 … $N
    for m in re.finditer(r"\b(?:bounty|reward|prize|payout)\b[^0-9$]{0,24}\$\s?(\d[\d,]*(?:\.\d+)?)\s*([kK])?", b, re.I):
        push(num(m.group(1), m.group(2)), "medium")
    # ② $N + 限定词：赏金名词=medium，裸币种=weak（预算行 "$600 USD" 就靠这条降级）
    for m in re.finditer(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*([kK])?\s*(USD[CT]?|USDG|SOL|bounty|reward|prize|payout)\b", b, re.I):
        noun = bool(re.fullmatch(r"bounty|reward|prize|payout", m.group(3), re.I))
        push(num(m.group(1), m.group(2)), "medium" if noun else "weak")
    # ③ 赏金词 … N USDC（无 $）
    for m in re.finditer(r"\b(?:bounty|reward|prize|payout)\b[^0-9]{0,24}(\d[\d,]*(?:\.\d+)?)\s*([kK])?\s*(?:USDC|USDT|USDG|USD|SOL)\b", b, re.I):
        push(num(m.group(1), m.group(2)), "medium")
    # ④ 方括号金额 [$50 ...]
    for m in re.finditer(r"\[\s*\$\s?(\d[\d,]*(?:\.\d+)?)\s*([kK])?", b):
        push(num(m.group(1), m.group(2)), "medium")
    # ⑤ 泛化付款动词 paid/pays $N → weak
    for m in re.finditer(r"\b(?:paid|pays?|paying)\b[^0-9$]{0,16}\$\s?(\d[\d,]*(?:\.\d+)?)\s*([kK])?", b, re.I):
        push(num(m.group(1), m.group(2)), "weak")
    return best, tier


def is_grant(title: str, body: str) -> bool:
    return bool(GRANT_TITLE.search(title or "") or GRANT_BODY.search((body or "")[:8000]))


def search(query: str, per_page: int = 50):
    q = urllib.parse.urlencode(
        {"q": query, "sort": "created", "order": "desc", "per_page": per_page}
    )
    d = http_json(f"{API}/search/issues?{q}", {"Authorization": f"Bearer {TOKEN}"})
    if "__err" in d:
        return []
    return d.get("items", [])


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"seen": {}}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def push_feishu(lines: list[str]) -> None:
    if not FEISHU:
        log("  · 未配置 FEISHU_WEBHOOK，跳过推送")
        return
    text = "\n".join(lines)
    payload = {"msg_type": "text", "content": {"text": text}}
    req = urllib.request.Request(FEISHU, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=json.dumps(payload).encode("utf-8"), timeout=20) as r:
            log(f"  · 飞书推送 HTTP {r.status}")
    except Exception as e:
        log(f"  ! 飞书推送失败: {type(e).__name__} {e}")


def main() -> int:
    if not TOKEN:
        log("FATAL: 缺少 GITHUB_TOKEN")
        return 2

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    state = load_state()
    seen = state.setdefault("seen", {})

    stats = {"scanned": 0, "dup": 0, "noise": 0, "grant": 0, "no_amount": 0,
             "weak": 0, "low": 0, "assigned": 0}
    hits = []
    uniq = set()

    for q in QUERIES:
        items = search(q)
        log(f"查询 {q!r} → {len(items)} 条")
        for it in items:
            repo = it["repository_url"].replace("https://api.github.com/repos/", "")
            num = it["number"]
            key = f"{repo}#{num}"
            if key in uniq:
                continue
            uniq.add(key)
            stats["scanned"] += 1

            if it.get("state") != "open":
                continue
            created = datetime.fromisoformat(it["created_at"].replace("Z", "+00:00"))
            if created < cutoff:
                continue

            title, body = it.get("title", ""), it.get("body") or ""

            if AGGREGATOR_REPO.search(repo) or AGGREGATOR_TITLE.search(title):
                stats["noise"] += 1
                continue

            # 资助申请门：没有外部贡献者付费证据就整条丢掉
            if is_grant(title, body):
                if not GRANT_EXTERNAL_PAYOUT.search(f"{title}\n{body}"):
                    stats["grant"] += 1
                    continue

            amount, tier = extract_amount(f"{title}\n{body}")
            labels = [l["name"] for l in it.get("labels", [])]
            # 标签里的 $N 是强证据
            for lb in labels:
                m = re.fullmatch(r"\$\s?([\d.,]+)\s*(k)?", lb.strip(), re.I)
                if m:
                    v = float(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1)
                    amount, tier = v, "strong"

            if amount is None:
                stats["no_amount"] += 1
                continue
            if FICTIONAL.search(f"{title}\n{body}") and tier != "strong":
                stats["noise"] += 1
                continue
            if tier == "weak" and not PAYOUT_PLATFORM.search(f"{title}\n{body}"):
                stats["weak"] += 1
                continue
            if amount < MIN_AMOUNT:
                stats["low"] += 1
                continue
            if it.get("assignees"):
                stats["assigned"] += 1
                continue

            if key in seen:
                stats["dup"] += 1
                continue

            hits.append({
                "key": key,
                "repo": repo,
                "num": num,
                "title": title,
                "amount": amount,
                "tier": tier,
                "comments": it.get("comments", 0),
                "labels": labels,
                "url": it["html_url"],
                "created": it["created_at"],
            })

    hits.sort(key=lambda x: (-x["amount"], x["created"]))

    os.makedirs(OUT_DIR, exist_ok=True)
    day = now.strftime("%Y-%m-%d")
    md_path = os.path.join(OUT_DIR, f"digest-{day}.md")

    lines = [f"# 悬赏摘要 {day}（近 {WINDOW_HOURS}h）", ""]
    lines.append(
        f"扫描 {stats['scanned']} · 新命中 {len(hits)} · 重复 {stats['dup']} · "
        f"资助申请 {stats['grant']} · 噪声 {stats['noise']} · 无金额 {stats['no_amount']} · "
        f"弱证据 {stats['weak']} · 金额不足 {stats['low']} · 已指派 {stats['assigned']}"
    )
    lines.append("")
    if hits:
        lines.append("| 金额 | 仓库 | 标题 | 评论 | 等级 |")
        lines.append("|---|---|---|---|---|")
        for h in hits:
            t = h["title"].replace("|", "\\|")[:60]
            lines.append(
                f"| ${h['amount']:.0f} | {h['repo']} | [{t}]({h['url']}) | {h['comments']} | {h['tier']} |"
            )
    else:
        lines.append("本期无新单。")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log(f"已写入 {md_path}")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    if hits:
        push_feishu([
            f"💰 悬赏摘要 {day}｜新命中 {len(hits)} 条",
            *[f"${h['amount']:.0f}  {h['repo']}#{h['num']}  {h['title'][:50]}" for h in hits[:8]],
            "详见仓库 Actions 运行摘要",
        ])

    for h in hits:
        seen[h["key"]] = {"at": now.isoformat(), "amount": h["amount"]}
    save_state(state)

    log(
        f"完成：扫描 {stats['scanned']} · 新命中 {len(hits)} · 资助申请拦截 {stats['grant']} · "
        f"弱证据拦截 {stats['weak']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
