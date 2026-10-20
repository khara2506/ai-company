#!/usr/bin/env python3
"""手元の版（portal.html）と外に出す版（share.html）を作る（決まった処理）。

流れ:
  1. 前回の share.html を消す
  2. portal.html（実名入り・手元用）を書く
  3. 外に出す版をメモリの中で組み立て、画面に出る文字だけを禁止語と照合する
       当たった -> share.html は書かず、端末に語と場所を出して止まる（終了コード 3）
  4. 載る文字列の一覧と承認の印（APPROVE_CODE）を端末に出す（STATUS: awaiting-approval）
  5. --approve <印> で再実行すると share.html を書く（本人の OK の後）。見せたあとで中身が変わっていれば書かない

どこにも送らない。Python 3.9 以上・標準ライブラリだけ。
"""
import argparse
import html as htmllib
import hashlib
import json
import os
import re
import subprocess
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_SHORT = "github.com/khara2506/ai-company"
SKILL_DOC_URL = "https://code.claude.com/docs/en/skills"
DEFAULT_COMPANY = "わたしの AI 会社"
WINDOW_DAYS = 30
JOB_MIN = 5
SMALL_SAMPLE = 30  # 抜き出しがこれより少ないときは全件へ引き伸ばさない（少ない材料からの推定は外れやすい）
JOB_MAX = 3
MAX_DEPTS = 7
ROSTER_LIMIT = 10
FONT_STACK = '"Hiragino Sans","Yu Gothic UI","Meiryo","Noto Sans CJK JP",sans-serif'

C = {  # 配る版の DESIGN.md の色
    "paper": "#fffdf8", "sheet": "#f7f2e7", "rule": "#e4dccb", "rule_strong": "#cfc4ad",
    "text": "#1c1917", "muted": "#57534e", "faint": "#8a837a", "primary": "#1e3a8a",
    "seal": "#b4232a", "info": "#e8edf8",
}


def load_vocab():
    with open(os.path.join(HERE, "vocab.json"), encoding="utf-8") as f:
        return json.load(f)


def esc(s):
    return htmllib.escape(str(s), quote=True)


def d2s(d):
    return d.isoformat() if d else ""


def pdate(s):
    try:
        return date.fromisoformat(s[:10]) if s else None
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------- org.json

def letters_for(n):
    """0->A ... 25->Z, 26->AA, 27->AB ...（R2-16）"""
    s = ""
    n += 1
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, type(default)) else default
    except (OSError, ValueError):
        return default


def clean_company_name(s):
    s = re.sub(r"[\x00-\x1f\x7f<>]", "", str(s or "")).strip()
    return s[:40]


def normalize_org(org, company, vocab, warn=lambda m: None):
    """org.json に足りない所を既定で埋め、決まった一覧に無い値を直す。職名の記号を割り当てる。"""
    org = dict(org) if isinstance(org, dict) else {}
    org.setdefault("version", 1)
    org["company_name"] = clean_company_name(org.get("company_name"))
    people = org["people"] = dict(org.get("people") or {})
    dept_ids = {d["id"] for d in vocab["departments"]}
    for sid, p in list(people.items()):
        if not isinstance(p, dict):
            p = {}
        if p.get("dept") not in dept_ids:
            if p.get("dept"):
                warn("部署の名前が一覧に無いので「総務部」にしました")
            p["dept"] = "general"
        p["real_name_allowed"] = bool(p.get("real_name_allowed"))
        people[sid] = p
    org["letters"] = {k: dict(v) for k, v in (org.get("letters") or {}).items() if isinstance(v, dict)}
    org["agent_letters"] = dict(org.get("agent_letters") or {})
    org["allowed_terms"] = [str(t) for t in (org.get("allowed_terms") or [])]
    work_ids = {w["id"] for w in vocab["works"]}
    jobs = {}
    for j in org.get("jobs") or []:
        if not isinstance(j, dict):
            continue
        w = j.get("work") if j.get("work") in work_ids else vocab["other_work"]["id"]
        if j.get("work") != w:
            warn("作業の種類が一覧に無いので「その他の定型作業」にしました")
        try:
            cnt = max(0, int(j.get("count", 0)))
        except (TypeError, ValueError):
            cnt = 0
        cur = jobs.setdefault(w, {"work": w, "count": 0, "covered": True})
        cur["count"] += cnt
        cur["covered"] = cur["covered"] and bool(j.get("covered"))
    org["jobs"] = sorted(jobs.values(), key=lambda x: x["work"])

    emps = sorted(company.get("skills", []), key=lambda s: (s.get("joined") or "9999", s["id"]))
    retired = sorted(company.get("retired", []), key=lambda s: (s.get("joined") or "9999", s["id"]))
    for s in emps + retired:
        sid = s["id"]
        if s.get("public"):
            continue
        dept = people.get(sid, {}).get("dept", "general")
        people.setdefault(sid, {"dept": dept, "real_name_allowed": False})
        lm = org["letters"].setdefault(dept, {})
        if sid not in lm:
            lm[sid] = letters_for(len(lm))
    for a in sorted(a["id"] for a in company.get("agents", []) if a.get("own")):
        if a not in org["agent_letters"]:
            org["agent_letters"][a] = letters_for(len(org["agent_letters"]))
    return org


# ---------------------------------------------------------------- モデル

def month_key(d):
    return "%04d-%02d" % (d.year, d.month)


def build_model(company, org, vocab, history_rows=None):
    today = pdate(company["today"])
    founded = pdate(company["founded"]) or today
    depts = {d["id"]: d for d in vocab["departments"]}
    works = {w["id"]: w for w in vocab["works"]}
    works[vocab["other_work"]["id"]] = vocab["other_work"]
    people = org["people"]
    win_start = today - timedelta(days=WINDOW_DAYS - 1)

    def person(sid, pub):
        p = people.get(sid, {})
        dept = "general" if pub and sid not in people else p.get("dept", "general")
        letter = org["letters"].get(dept, {}).get(sid, "")
        title = depts[dept]["title"] + letter
        allowed = bool(p.get("real_name_allowed"))
        share = sid if (pub or allowed) else title
        return dept, title, share

    emps = []
    for s in company.get("skills", []):
        dept, title, share = person(s["id"], s["public"])
        j, lu = pdate(s.get("joined")), pdate(s.get("last_used"))
        base = lu or j
        status = "休職" if base and (today - base).days >= WINDOW_DAYS else "在籍"
        emps.append({"id": s["id"], "kind": s["kind"], "public": s["public"], "dept": dept,
                     "title": title, "local": s["id"], "share": share, "joined": j, "last_used": lu,
                     "n30": s.get("count_30d", 0), "total": s.get("count_total", 0), "status": status,
                     "real_allowed": bool(people.get(s["id"], {}).get("real_name_allowed")),
                     "manager": bool(s.get("manager"))})
    retired = []
    for s in company.get("retired", []):
        dept, title, share = person(s["id"], False)
        retired.append({"id": s["id"], "dept": dept, "title": title, "local": s["id"], "share": share,
                        "joined": pdate(s.get("joined")), "retired_on": pdate(s.get("retired_on"))})

    # 部署: 社員のいる部署だけ・最大7（R2-15）
    counts = {}
    for e in emps:
        counts[e["dept"]] = counts.get(e["dept"], 0) + 1
    order = [d["id"] for d in vocab["departments"]]
    ranked = sorted(counts, key=lambda k: (-counts[k], order.index(k)))
    keep = ranked if len(ranked) <= MAX_DEPTS else ranked[:MAX_DEPTS - 1]
    for e in emps:
        e["dept_show"] = e["dept"] if e["dept"] in keep else "other"
    dept_rows = [(k, depts[k]["name"], counts[k]) for k in keep]
    if len(ranked) > MAX_DEPTS:
        dept_rows.append(("other", "その他", sum(counts[k] for k in ranked[MAX_DEPTS - 1:])))
    dept_name = {k: n for k, n, _ in dept_rows}

    top = sorted([e for e in emps if e["n30"] > 0], key=lambda e: (-e["n30"], e["id"]))[:3]
    new_hires = [e for e in emps if e["joined"] and e["joined"] >= win_start]

    agents = []
    for a in sorted(company.get("agents", []), key=lambda a: (-a["count_30d"], a["id"])):
        if a.get("own"):
            alias = "外部スタッフ" + org["agent_letters"].get(a["id"], "")
            share, local = alias, "%s（%s）" % (a["id"], alias)
        else:
            share = local = a["id"]
        agents.append({"id": a["id"], "local": local, "share": share, "n30": a["count_30d"],
                       "total": a["count_total"], "builtin": a.get("builtin", False)})

    typed = company.get("typed", {})
    total, sample_n = typed.get("total_30d", 0), typed.get("sample_n", 0)
    sampled = total > sample_n > 0
    small = sampled and sample_n < SMALL_SAMPLE
    prefix = "少なくとも" if small else ("約" if sampled else "")
    jobs = []
    for j in org["jobs"]:
        if j["covered"] or j["work"] == vocab["other_work"]["id"] or sample_n <= 0:
            continue
        est = round(j["count"] * total / sample_n) if sampled and not small else j["count"]
        if est >= JOB_MIN:
            w = works[j["work"]]
            jobs.append({"work": w["name"], "dept": depts[w["dept"]]["title"], "est": est, "sampled": sampled,
                         "prefix": prefix})
    jobs.sort(key=lambda x: (-x["est"], x["work"]))
    jobs = jobs[:JOB_MAX]

    stats = company.get("stats", {})
    has_records = bool(stats.get("files")) and bool(company["range"].get("oldest"))
    rows = history_rows or []
    hist_oldest = company.get("history_range", {}).get("oldest")
    oldest = company["range"].get("oldest")
    partial_note = None
    if oldest and hist_oldest and pdate(hist_oldest) < pdate(oldest):
        partial_note = d2s(pdate(oldest) - timedelta(days=1))
    return {
        "name": org["company_name"] or DEFAULT_COMPANY, "today": today, "founded": founded,
        "period": (today - founded).days // 365 + 1, "days": (today - founded).days,
        "oldest": pdate(oldest), "newest": pdate(company["range"].get("newest")),
        "partial_note": partial_note, "has_records": has_records,
        "retention_days": company.get("retention_days"),
        "emps": emps, "retired": retired, "dept_rows": dept_rows, "dept_name": dept_name,
        "top": top, "new_hires": new_hires, "agents": agents, "jobs": jobs,
        "typed_found": bool(typed.get("found")), "sampled": sampled, "small_sample": small,
        "timeline": build_timeline(emps, retired, founded, today, rows, dept_name),
        "has_typed_data": has_records and bool(typed.get("found")),
    }


def count_at(emps, retired, d):
    n = sum(1 for e in emps if e["joined"] and e["joined"] <= d)
    n += sum(1 for r in retired if r["joined"] and r["joined"] <= d and (not r["retired_on"] or r["retired_on"] > d))
    return n


def leave_return_events(e, rows, today):
    """休職・復帰（実行ごとの記録 history.jsonl の最後に呼ばれた日の変化から）。"""
    seq = []
    for r in rows:
        v = (r.get("employees") or {}).get(e["id"])
        if v and v[1]:
            seq.append((pdate(r["date"]), pdate(v[1])))
    seq.append((today, e["last_used"]))
    events, on_leave = [], False
    for rd, lu in seq:
        if not rd or not lu:
            continue
        away = (rd - lu).days >= WINDOW_DAYS
        if away and not on_leave:
            events.append((lu + timedelta(days=WINDOW_DAYS), "休職", e))
            on_leave = True
        elif not away and on_leave:
            events.append((lu, "復帰", e))
            on_leave = False
    return events


def build_timeline(emps, retired, founded, today, rows, dept_name):
    events = []
    for e in emps + retired:
        if e["joined"]:
            events.append((e["joined"], "入社", e))
    for r in retired:
        if r["retired_on"]:
            events.append((r["retired_on"], "退職", r))
    for e in emps:
        events += leave_return_events(e, rows, today)
    events.append((founded, "創業", None))
    periods = []
    k = 1
    while True:
        start = founded + timedelta(days=365 * (k - 1))
        if start > today:
            break
        end = min(start + timedelta(days=364), today)
        periods.append({
            "n": k, "start": start, "end": end,
            "from": count_at(emps, retired, start),
            "to": count_at(emps, retired, end),
            "events": sorted([x for x in events if start <= x[0] <= end], key=lambda x: (x[0], x[1]), reverse=True),
        })
        k += 1
    return list(reversed(periods))


# ---------------------------------------------------------------- 文字の幅・折り返し

def char_w(ch):
    return 1.0 if unicodedata.east_asian_width(ch) in ("W", "F") else 0.58


def text_w(s, size):
    return sum(char_w(c) for c in s) * size * 1.15  # OS ごとの字幅の違いに15%の余裕


def wrap_lines(s, size, max_w):
    lines, cur = [], ""
    for ch in s:
        if cur and text_w(cur + ch, size) > max_w:
            cut = max(cur.rfind(c) for c in ":-_/ ")
            if cut > 0 and cut >= len(cur) // 2:
                lines.append(cur[:cut + 1])
                cur = cur[cut + 1:]
            else:
                lines.append(cur)
                cur = ""
        cur += ch
    if cur:
        lines.append(cur)
    return lines or [""]


def fit(s, size, max_w):
    if text_w(s, size) <= max_w:
        return s
    while s and text_w(s + "…", size) > max_w:
        s = s[:-1]
    return s + "…"


# ---------------------------------------------------------------- 画像（SVG）

HIRE_LABEL = {"own": "自作", "external": "中途入社（スキル集）", "plugin": "中途入社（プラグイン）"}

# この会社の見方（会社の言葉, Claude Code では）。固定の文なので外に出す版にも載せてよい
GLOSSARY = [
    ("代表（あなた）", "Claude に仕事を頼む人"),
    ("社員", "自分で作ったスキル。決まった仕事の手順を覚えた担当（例: 請求書づくり）"),
    ("まとめ役", "ほかのスキルやサブエージェントに仕事を割り振るスキル（例: 調べものを手分けして頼み、結果をまとめる）"),
    ("中途入社", "他の人が作ったスキル集やプラグインから入ったスキル"),
    ("外部スタッフ", "サブエージェントと、Claude Code に最初から入っているスキル。社員数には入れない"),
    ("求人", "手で何度も頼んでいるのに、担当するスキルがいない仕事"),
]


def headcount(emps):
    own = sum(1 for e in emps if e["kind"] == "own")
    return own, len(emps) - own, sum(1 for e in emps if e.get("manager"))


def alt_text(m, mode):
    name = m["name"]
    if not m["emps"]:
        return "%s。創業準備中です。最初の社員（スキル）を迎えましょう。" % name
    parts = ["%s。社員%d名" % (name, len(m["emps"]))]
    parts.append("部署は" + "、".join("%s%d名" % (n, c) for _, n, c in m["dept_rows"]))
    if m["top"]:
        parts.append("この30日に活躍した社員は" + "、".join(
            "%s（%d回）" % (e["share"], e["n30"]) for e in m["top"]))
    if m["jobs"]:
        j = m["jobs"][0]
        parts.append("求人は%s、%s（この30日で%s%d回、手で対応）" % (
            j["dept"], j["work"], j["prefix"], j["est"]))
    return "。".join(parts) + "。"


EMPTY_STEPS = ["毎回手で頼んでいる作業を1つ選ぶ", "Claude Code にスキルにしてもらう", "/ai-company を開き直すと社員1名"]


def build_svg(m, mode="share"):
    """1080x1350 の1枚。文字と図形だけで作る（foreignObject は使わない: E-3）。(svg文字列, 最下端のy) を返す。"""
    W, H, M = 1080, 1350, 72
    cw = W - 2 * M
    out = []
    add = out.append

    def txt(x, y, s, size, fill=C["text"], weight=400, anchor="start", extra=""):
        add('<text x="%d" y="%d" font-size="%d" font-weight="%d" fill="%s" text-anchor="%s"%s>%s</text>'
            % (x, y, size, weight, fill, anchor, extra, esc(s)))

    def rule(y, strong=False):
        add('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="2"/>' % (
            M, y, W - M, y, C["rule_strong"] if strong else C["rule"]))

    def seal(cx, cy, r, label):
        add('<circle cx="%d" cy="%d" r="%d" fill="none" stroke="%s" stroke-width="4"/>' % (cx, cy, r, C["seal"]))
        add('<text x="%d" y="%d" font-size="%d" font-weight="700" fill="%s" text-anchor="middle">%s</text>'
            % (cx, cy + int(r * 0.22), int(r * 0.62), C["seal"], esc(label)))

    add('<rect width="%d" height="%d" fill="%s"/>' % (W, H, C["paper"]))
    add('<rect x="24" y="24" width="%d" height="%d" fill="none" stroke="%s" stroke-width="2"/>' % (W - 48, H - 48, C["rule_strong"]))
    txt(M, 96, "AIC-ORG-001 ／ 創業 %s ／ 第%d期" % (d2s(m["founded"]), m["period"]), 24, C["faint"])
    size = 56
    lines = wrap_lines(m["name"], size, cw)
    if len(lines) > 1:
        size = 44
        lines = wrap_lines(m["name"], size, cw)[:2]
    y = 164
    for i, ln in enumerate(lines):
        txt(M, y + i * 56, ln, size, C["text"], 700)
    y += (len(lines) - 1) * 56 + 32
    rule(y, True)
    y += 8
    if not m["emps"]:
        txt(M, y + 120, "創業準備中", 100, C["primary"], 700)
        txt(M, y + 190, "最初の社員（スキル）を迎えましょう", 36, C["muted"])
        y += 240
        rule(y)
        y += 56
        txt(M, y, "はじめの3歩", 26, C["faint"])
        for i, step in enumerate(EMPTY_STEPS):
            y += 110
            txt(M, y, str(i + 1), 64, C["primary"], 700)
            txt(M + 96, y - 8, fit(step, 36, cw - 96), 36, C["text"], 700)
        y += 40
        rule(y)
    else:
        base = y + 108
        add('<text x="%d" y="%d" fill="%s" font-weight="700"><tspan font-size="44" fill="%s" font-weight="400">社員 </tspan>'
            '<tspan font-size="116" fill="%s">%d</tspan><tspan font-size="56" fill="%s"> 名</tspan></text>'
            % (M, base, C["primary"], C["muted"], C["primary"], len(m["emps"]), C["text"]))
        seal(W - M - 66, base - 44, 66, "在籍")
        txt(M, base + 58, "うち自作 %d名 ／ この30日の入社 %d名" % (headcount(m["emps"])[0], len(m["new_hires"])), 36, C["muted"])
        y = base + 80
        rule(y)
        y += 36
        txt(M, y, "部署", 26, C["faint"])
        mx = max(c for _, _, c in m["dept_rows"])
        y += 14
        for _, n, c in m["dept_rows"]:
            y += 44
            txt(M, y, n, 32)
            bw = max(8, int(440 * c / mx))
            add('<rect x="380" y="%d" width="%d" height="26" fill="%s"/>' % (y - 24, bw, C["primary"]))
            txt(380 + bw + 16, y, str(c), 32, C["text"], 700)
        y += 20
        rule(y)
        if m["top"]:
            y += 38
            txt(M, y, "活躍した社員（この30日）", 26, C["faint"])
            for i, e in enumerate(m["top"]):
                y += 42
                txt(M, y, str(i + 1), 34, C["faint"])
                txt(M + 56, y, fit(e["share"], 34, 600), 34, C["text"], 700)
                txt(W - M, y, "%d回" % e["n30"], 34, C["text"], 400, "end")
            y += 22
            rule(y)
    if m["jobs"]:
        j = m["jobs"][0]
        y += 24
        top = y
        box_h = 156
        add('<rect x="%d" y="%d" width="%d" height="%d" fill="none" stroke="%s" stroke-width="3"/>' % (M, top, cw, box_h, C["primary"]))
        txt(M + 28, top + 44, "求人", 26, C["faint"])
        title = fit("%s　%s" % (j["dept"], j["work"]), 40, cw - 56 - 150)
        txt(M + 28, top + 94, title, 40, C["text"], 700)
        txt(M + 28, top + 134, "この30日で%s%d回、手で対応" % (j["prefix"], j["est"]), 30, C["muted"])
        seal(W - M - 66, top + 70, 44, "募集")
        y = top + box_h
    y = max(y + 46, H - 112)  # 末尾の表記は下端にそろえる（中身が少ないときに下が空かない）
    rec = ("記録は %s 以降" % d2s(m["oldest"])) if m["oldest"] else "会話の記録なし"
    txt(M, y, "%s ・ 創業から%d日" % (rec, m["days"]), 26, C["muted"])
    txt(M, y + 40, "/ai-company　%s" % REPO_SHORT, 22, C["faint"])
    bottom = y + 40
    svg = ('<svg id="share-svg" xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
           'font-family=\'%s\' role="img" aria-label="%s" data-field="画像">%s</svg>'
           % (W, H, W, H, FONT_STACK, esc(alt_text(m, "share")), "".join(out)))
    return svg, bottom


# ---------------------------------------------------------------- ページ

CSS = """
:root{--paper:#fffdf8;--sheet:#f7f2e7;--rule:#e4dccb;--rule-strong:#cfc4ad;--text:#1c1917;--muted:#57534e;--faint:#8a837a;
--primary:#1e3a8a;--on-primary:#fff;--accent:#d97706;--accent-text:#b45f06;--accent-tint:#fdf1dc;--seal:#b4232a;--info:#e8edf8}
@media (prefers-color-scheme:dark){:root{--paper:#14120f;--sheet:#1d1a16;--rule:#34302a;--rule-strong:#4a443b;--text:#ece6da;
--muted:#b3ac9f;--faint:#8a8478;--primary:#9db2ec;--on-primary:#0f1730;--accent:#f0a43a;--accent-text:#f0a43a;--accent-tint:#3a2a12;--seal:#e0605a;--info:#1d2438}}
*{box-sizing:border-box}
html{scroll-behavior:smooth;scroll-padding-top:16px}
body{margin:0;background:var(--paper);color:var(--text);font:16px/1.75 "Noto Sans JP","Hiragino Sans","Yu Gothic UI","Meiryo","Noto Sans CJK JP",sans-serif;
overflow-wrap:anywhere;font-variant-numeric:tabular-nums;padding-bottom:84px}
.layout{display:block}
.col{padding:0 16px;max-width:720px;margin:0 auto}
.toc{display:none}
h1{font-size:clamp(24px,6vw,32px);line-height:1.35;margin:4px 0 8px}
h2{font-size:22px;line-height:1.4;margin:0 0 12px}
h3{font-size:16px;margin:24px 0 4px}
p{margin:8px 0}
.docline{font-size:12px;letter-spacing:.02em;color:var(--faint);margin:24px 0 0}
.small{font-size:13px;line-height:1.5;color:var(--muted)}
section{padding:8px 0 40px;border-top:1px solid var(--rule-strong)}
section>h2:first-child{margin-top:24px}
.notice{background:var(--accent-tint);border-left:4px solid var(--accent);padding:12px 16px;margin:16px 0;border-radius:4px}
.notice b{color:var(--accent-text);margin-right:8px}
.band{background:var(--info);padding:8px 16px;font-size:13px;margin:12px 0;border-radius:4px}
dl.facts{display:grid;grid-template-columns:auto 1fr;gap:0;margin:16px 0;border-top:1px solid var(--rule)}
dl.facts dt,dl.facts dd{margin:0;padding:10px 0;border-bottom:1px solid var(--rule);min-height:44px}
dl.facts dt{color:var(--muted);padding-right:16px}
dl.facts dd{text-align:right;font-weight:700}
.seal{display:inline-block;width:52px;height:52px;line-height:46px;text-align:center;border:3px solid var(--seal);border-radius:50%;color:var(--seal);font-weight:700;font-size:15px}
.rep{margin:12px 0 4px;font-weight:700}
.tree{border-left:2px solid var(--rule-strong);margin-left:8px;padding-left:12px}
details{border-bottom:1px solid var(--rule)}
summary{display:flex;align-items:center;gap:12px;min-height:44px;cursor:pointer;list-style:none}
summary::-webkit-details-marker{display:none}
summary::after{content:"›";margin-left:auto;color:var(--faint)}
details[open]>summary::after{content:"⌄"}
.dn{flex:0 0 7.5em}
.bar{flex:1;height:14px;background:var(--sheet)}
.bar i{display:block;height:100%;background:var(--primary)}
.num{font-weight:700;min-width:2em;text-align:right}
ul.plain{list-style:none;margin:0;padding:0 0 8px 0}
ul.plain li{padding:6px 0;border-top:1px solid var(--rule)}
.meta{color:var(--muted);font-size:13px}
.idcard summary{flex-wrap:wrap;column-gap:12px;row-gap:0;padding:6px 0}
.idcard .meta{order:3;flex-basis:100%}
details.glossary{border:0;margin:0 0 12px}
details.glossary>summary{color:var(--primary);font-weight:700}
details.glossary dl{margin:0 0 8px;padding:8px 16px;background:var(--sheet);border-left:4px solid var(--rule-strong)}
details.glossary dt{font-weight:700;margin-top:8px}
details.glossary dd{margin:0;color:var(--muted);font-size:14px;line-height:1.6}
.idcard{border:0;border-bottom:1px solid var(--rule)}
.idcard .body{background:var(--sheet);border:1px solid var(--rule-strong);border-radius:12px;padding:12px 16px;margin:4px 0 12px;box-shadow:0 8px 24px -12px rgba(0,0,0,.25)}
.idcard .body .head{font-size:12px;color:var(--faint);border-bottom:1px solid var(--rule-strong);padding-bottom:4px;margin-bottom:8px}
.idcard dl{display:grid;grid-template-columns:auto 1fr;gap:2px 16px;margin:0}
.idcard dt{color:var(--muted)}.idcard dd{margin:0}
table{border-collapse:collapse;width:100%;margin:8px 0}
th,td{border-bottom:1px solid var(--rule);padding:8px 4px;text-align:left;font-size:14px}
td.r,th.r{text-align:right}
.period{margin:16px 0 4px;font-weight:700;font-size:18px}
.month{margin:12px 0 0;font-size:13px;color:var(--muted)}
.ev{padding:6px 0;border-top:1px solid var(--rule)}
.ev b{display:inline-block;min-width:3em;margin-right:8px}
.job{border:2px solid var(--primary);padding:16px;margin:16px 0}
.job h3{margin:0 0 4px}
.job .kv{margin:4px 0}
.prompt{background:var(--sheet);border:1px solid var(--rule-strong);border-radius:4px;padding:12px;font-size:14px;line-height:1.6}
button,a.btn{min-height:44px;padding:8px 16px;border-radius:8px;font:inherit;cursor:pointer;display:inline-flex;align-items:center;text-decoration:none}
button.primary{background:var(--primary);color:var(--on-primary);border:0}
button.secondary,a.btn{background:transparent;color:var(--primary);border:1px solid var(--primary)}
a{color:var(--primary)}
:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
figure{margin:16px 0;text-align:center}
figure svg{width:100%;max-width:360px;height:auto;border:1px solid var(--rule-strong)}
.empty{border:1px dashed var(--rule-strong);padding:16px;margin:12px 0;border-radius:4px}
footer{padding:24px 0;font-size:13px;color:var(--muted)}
.tabbar{position:fixed;left:0;right:0;bottom:0;background:var(--sheet);border-top:1px solid var(--rule-strong);display:flex;padding-bottom:env(safe-area-inset-bottom)}
.tabbar a{flex:1;min-height:56px;display:flex;align-items:center;justify-content:center;color:var(--faint);text-decoration:none;font-size:13px}
.tabbar a[aria-current=true]{color:var(--primary);font-weight:700}
@media (min-width:720px){
 body{padding-bottom:0}
 .layout{display:flex;max-width:1040px;margin:0 auto;gap:32px}
 .toc{display:block;position:sticky;top:0;align-self:flex-start;width:160px;padding-top:32px}
 .toc a{display:flex;align-items:center;min-height:44px;color:var(--text);text-decoration:none}
 .toc a[aria-current=true]{color:var(--primary);font-weight:700}
 .col{flex:1;padding:0}
 .tabbar{display:none}
}
@media (prefers-reduced-motion:reduce){html{scroll-behavior:auto}*{transition:none!important;animation:none!important}}
"""

SECTIONS = [("overview", "概要", "会社概要", "HOME"), ("org", "組織", "組織図", "ORG"),
            ("roster", "名簿", "社員名簿", "ROS"), ("history", "社史", "社史", "HST"), ("jobs", "求人", "求人票", "JOB")]

JS_COMMON = """
(function(){
var links=document.querySelectorAll('[data-nav]');
function mark(id){links.forEach(function(a){if(a.getAttribute('href')==='#'+id){a.setAttribute('aria-current','true')}else{a.removeAttribute('aria-current')}})}
mark('overview');
if('IntersectionObserver' in window){var io=new IntersectionObserver(function(es){es.forEach(function(e){if(e.isIntersecting){mark(e.target.id)}})},{rootMargin:'-20% 0px -70% 0px'});
document.querySelectorAll('main section').forEach(function(s){io.observe(s)})}
document.querySelectorAll('[data-copy]').forEach(function(b){b.addEventListener('click',function(){
var t=document.getElementById(b.getAttribute('data-copy')).innerText;var done=function(){var o=b.getAttribute('data-label')||b.textContent;b.setAttribute('data-label',o);b.textContent='コピーしました';setTimeout(function(){b.textContent=o},2000)};
if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(t).then(done,function(){fallback(t,done)})}else{fallback(t,done)}})});
function fallback(t,done){var a=document.createElement('textarea');a.value=t;document.body.appendChild(a);a.select();try{document.execCommand('copy');done()}catch(e){}a.remove()}
})();
"""

JS_SAVE = """
(function(){
var btn=document.getElementById('save-img');if(!btn)return;
var msg=document.getElementById('save-msg');
function fail(url){msg.textContent='保存できませんでした。下の画像を長押し（右クリック）して保存してください。';
var i=document.createElement('img');i.src=url;i.alt='';i.style.maxWidth='100%';i.style.height='auto';msg.appendChild(document.createElement('br'));msg.appendChild(i)}
btn.addEventListener('click',function(){
var svg=document.getElementById('share-svg').cloneNode(true);svg.removeAttribute('style');svg.setAttribute('width','1080');svg.setAttribute('height','1350');
var s=new XMLSerializer().serializeToString(svg);
var url='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(s);
var img=new Image();
img.onload=function(){try{var c=document.createElement('canvas');c.width=1080;c.height=1350;c.getContext('2d').drawImage(img,0,0,1080,1350);
c.toBlob(function(b){if(!b){fail(url);return}var a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='ai-company.png';document.body.appendChild(a);a.click();a.remove();msg.textContent='保存しました'},'image/png')}catch(e){fail(url)}};
img.onerror=function(){fail(url)};img.src=url});
})();
"""


def doc_no(code, n=1):
    return "AIC-%s-%03d" % (code, n)


def person_label(e, mode):
    if mode == "share" or e.get("public"):
        return e["share"]
    return "%s（%s）" % (e["local"], e["title"])


def job_prompt(j):
    return ("「%s」を、ふだん手で何度も頼んでいます（この30日で%s%d回）。"
            "この作業を自動でやってくれる Claude Code のスキルを作ってください。"
            "最初に、いつもどう頼んでいるかを私に聞いてから、SKILL.md の案を出してください。"
            % (j["work"], j["prefix"], j["est"]))


EMPTY_PROMPT = ("Claude Code で毎回手で頼んでいる作業を1つ選んで、それを自動でやってくれるスキルを作ってください。"
                "最初に、いつもどう頼んでいるかを私に聞いてから、SKILL.md の案を出してください。")


def build_html(m, mode, svg=None):
    share = mode == "share"
    name_html = '<span data-field="会社名">%s</span>' % esc(m["name"])
    out = []
    w = out.append
    w('<!doctype html>\n<html lang="ja"><head><meta charset="utf-8">'
      '<meta name="viewport" content="width=device-width,initial-scale=1">'
      '<meta name="color-scheme" content="light dark">'
      '<title>%s %s</title><style>%s</style></head><body>' % (
          esc(m["name"]), "社内ポータル" if share else "社内ポータル（手元の版）", CSS))
    w('<div class="layout"><nav class="toc" aria-label="目次">%s</nav><div class="col">' % "".join(
        '<a href="#%s" data-nav>%s</a>' % (i, esc(t)) for i, _, t, _ in SECTIONS))
    w('<header><p class="docline">%s ／ 創業 %s ／ 第%d期</p><h1>%s</h1></header><main>' % (
        doc_no("HOME"), d2s(m["founded"]), m["period"], name_html))
    emps = m["emps"]

    def sec_open(sid, title, code=None):
        line = '<p class="docline">%s</p>' % doc_no(code) if code else ""
        w('<section id="%s" aria-labelledby="h-%s">%s<h2 id="h-%s">%s</h2>' % (sid, sid, line, sid, esc(title)))

    # 会社概要（書類番号はページ上部と同じなので出さない）
    sec_open("overview", "会社概要")
    if not share:
        w('<div class="notice" role="note"><b>注意</b>この版は実名入りです。人に送らないでください（共有用は share.html）。</div>')
    own_n, hired_n, mgr_n = headcount(emps)
    staff = "%d名（自作 %d・中途入社 %d）" % (len(emps), own_n, hired_n) if emps else "0名"
    facts = [("社員", staff), ("部署", "%d" % len(m["dept_rows"]))]
    if mgr_n:
        facts.append(("まとめ役", "%d名" % mgr_n))
    facts += [("外部スタッフ", "%d" % len(m["agents"])), ("創業", d2s(m["founded"])), ("期", "第%d期" % m["period"])]
    w('<dl class="facts">%s</dl>' % "".join("<dt>%s</dt><dd>%s</dd>" % (esc(k), esc(v)) for k, v in facts))
    w('<details class="glossary"><summary>この会社の見方（言葉の説明）</summary><dl>%s</dl></details>' % "".join(
        "<dt>%s</dt><dd>%s</dd>" % (esc(k), esc(v)) for k, v in GLOSSARY))
    if emps:
        w('<p><span class="seal" aria-hidden="true">在籍</span></p>')
    if m["has_records"]:
        note = "記録は %s 以降" % d2s(m["oldest"])
        if m["partial_note"]:
            note += "。%s までは前回までの記録から" % m["partial_note"]
        w('<p class="band">%s</p>' % esc(note))
    else:
        w('<p class="band">会話の記録がありません</p>')
    if share and svg:
        alt = alt_text(m, mode)
        w('<figure>%s</figure><div class="actions"><button class="primary" id="save-img" type="button">画像で保存</button>'
          '<button class="secondary" type="button" data-copy="alt-text">画像の説明文をコピー</button></div>'
          '<p class="small" id="save-msg" role="status"></p><p class="small" id="alt-text">%s</p>' % (svg, esc(alt)))
    w("</section>")

    # 組織図
    sec_open("org", "組織図", "ORG")
    if not emps:
        w('<div class="empty"><p><b>まだ社員がいません</b></p><p>最初の社員（スキル）を作りましょう。Claude Code に次の依頼文を貼ってください。</p>'
          '<p class="prompt" id="first-prompt">%s</p><div class="actions"><button class="primary" type="button" data-copy="first-prompt">依頼文をコピー</button>'
          '<a class="btn" href="%s">スキルの作り方（公式）</a></div></div>' % (esc(EMPTY_PROMPT), SKILL_DOC_URL))
    else:
        mx = max(c for _, _, c in m["dept_rows"])
        w('<p class="rep">代表（あなた）</p><div class="tree">')
        for did, dn, c in m["dept_rows"]:
            members = [e for e in emps if e["dept_show"] == did]
            w('<details><summary><span class="dn">%s</span><span class="bar" aria-hidden="true"><i style="width:%d%%"></i></span>'
              '<span class="num">%d</span></summary><ul class="plain">%s</ul></details>' % (
                  esc(dn), max(3, round(100 * c / mx)), c,
                  "".join("<li>%s</li>" % esc(person_label(e, mode)) for e in sorted(members, key=lambda e: e["title"]))))
        w("</div>")
    w("</section>")

    # 社員名簿
    sec_open("roster", "社員名簿", "ROS")
    if not emps:
        w('<div class="empty">まだ社員がいません。</div>')
    for did, dn, c in m["dept_rows"]:
        members = sorted([e for e in emps if e["dept_show"] == did], key=lambda e: (e["joined"] or date.max, e["id"]))
        w('<h3>%s（%d名）</h3>' % (esc(dn), c))

        def card(e):
            hire = HIRE_LABEL.get(e["kind"], "自作")
            meta = "%s%s %s ・ この30日 %d回" % ("まとめ役 ・ " if e.get("manager") else "", hire.split("（")[0],
                                               d2s(e["joined"]) or "入社日不明", e["n30"])
            role = ('<dt>役割</dt><dd>まとめ役（ほかの社員や外部スタッフに仕事を割り振る）</dd>' if e.get("manager") else "")
            return ('<details class="idcard"><summary><span>%s</span><span class="meta">%s</span></summary>'
                    '<div class="body"><div class="head">AI 社員証</div><dl><dt>名前</dt><dd>%s</dd><dt>所属</dt><dd>%s</dd>'
                    '<dt>採用</dt><dd>%s</dd>%s'
                    '<dt>入社日</dt><dd>%s</dd><dt>この30日</dt><dd>%d回</dd><dt>通算</dt><dd>%d回</dd><dt>状態</dt><dd>%s</dd></dl></div></details>'
                    % (esc(person_label(e, mode)), esc(meta),
                       esc(person_label(e, mode)), esc(m["dept_name"].get(did, dn)), esc(hire), role, esc(d2s(e["joined"]) or "不明"),
                       e["n30"], e["total"], esc(e["status"])))
        w("".join(card(e) for e in members[:ROSTER_LIMIT]))
        if len(members) > ROSTER_LIMIT:
            w('<details><summary>ほか%d名を表示</summary>%s</details>' % (
                len(members) - ROSTER_LIMIT, "".join(card(e) for e in members[ROSTER_LIMIT:])))
    w('<h3>外部スタッフ（社員数に含めません）</h3>')
    if m["agents"]:
        w('<table><tr><th>名前</th><th class="r">この30日</th><th class="r">通算</th></tr>%s</table>' % "".join(
            "<tr><td>%s</td><td class=\"r\">%d回</td><td class=\"r\">%d回</td></tr>" % (
                esc(a["share"] if share else a["local"]), a["n30"], a["total"]) for a in m["agents"]))
    else:
        w('<p class="small">サブエージェント・組み込みのスキルの呼び出しは記録にありません。</p>')
    if m["retired"]:
        w('<h3>退職</h3><table><tr><th>名前</th><th class="r">退職日</th></tr>%s</table>' % "".join(
            "<tr><td>%s</td><td class=\"r\">%s</td></tr>" % (
                esc(r["share"] if share else r["local"]), esc(d2s(r["retired_on"]))) for r in m["retired"]))
    w("</section>")

    # 社史
    sec_open("history", "社史", "HST")
    if not m["has_records"]:
        w('<div class="empty"><p>会話の記録がありません。Claude Code は既定で30日で記録を消します。</p>'
          '<p class="small">記録を長く残すには、設定ファイル（settings.json）の cleanupPeriodDays を大きくします。</p></div>')
    for p in m["timeline"]:
        w('<p class="period">第%d期 社員%d名→%d名</p><p class="small">%s 〜 %s</p>' % (
            p["n"], p["from"], p["to"], d2s(p["start"]), d2s(p["end"])))
        by_month = {}
        for ev in p["events"]:
            by_month.setdefault(month_key(ev[0]), []).append(ev)
        for mk in sorted(by_month, reverse=True):
            w('<p class="month">%s</p>' % mk)
            groups = {}
            for d, kind, e in by_month[mk]:
                groups.setdefault((d if kind == "創業" else mk, kind), []).append((d, e))
            for (_, kind), lst in sorted(groups.items(), key=lambda kv: (kv[1][0][0], kv[0][1]), reverse=True):
                if kind == "創業":
                    w('<div class="ev"><b>創業</b>%s</div>' % esc(d2s(lst[0][0])))
                    continue
                names = [person_label(e, mode) for _, e in sorted(lst, key=lambda x: (x[0], x[1]["id"]))]
                shown = names[:6]
                more = "（ほか%d名）" % (len(names) - 6) if len(names) > 6 else ""
                dates = sorted({d2s(d) for d, _ in lst})
                w('<div class="ev"><b>%s</b>%s %s%s</div>' % (
                    esc(kind), esc(dates[0] if len(dates) == 1 else "%s〜%s" % (dates[0], dates[-1])),
                    esc("、".join(shown)), esc(more)))
    w("</section>")

    # 求人票
    sec_open("jobs", "求人票", "JOB")
    if not m["has_records"]:
        w('<div class="empty"><p>会話の記録がありません。Claude Code は既定で30日で記録を消します。</p></div>')
    elif not m["has_typed_data"]:
        w('<div class="empty"><p>人が打った文を見つけられませんでした。求人票は作れません。</p></div>')
    elif not m["jobs"]:
        w('<div class="empty"><p>今は求人はありません。手で頼んでいる作業で、この30日に%d回以上のものは見つかりませんでした。</p></div>' % JOB_MIN)
    else:
        if m["small_sample"]:
            w('<p class="small">人が打った文が少ないため、抜き出しの中で数えた回数です（実際はもっと多いことがあります）。</p>')
        elif m["sampled"]:
            w('<p class="small">人が打った文の抜き出し（最大300件）からの推定です。</p>')
        for i, j in enumerate(m["jobs"]):
            w('<div class="job"><h3>求人：%s</h3><p class="kv">仕事の内容：%s</p><p class="kv">この30日で%s%d回、手で対応</p>'
              '<p class="small">いちばん大事なこと：この作業をスキルにする</p>'
              '<p class="prompt" id="jp-%d">%s</p><div class="actions"><button class="primary" type="button" data-copy="jp-%d">依頼文をコピー</button>'
              '<a class="btn" href="%s">スキルの作り方（公式）</a></div></div>' % (
                  esc(j["dept"]), esc(j["work"]), j["prefix"], j["est"], i, esc(job_prompt(j)), i, SKILL_DOC_URL))
    w("</section></main>")
    w("<footer>")
    if share:
        w("<p>自作スキルの名前は仮の職名に置き換えています。</p>")
    w("<p>/ai-company ・ %s</p></footer></div></div>" % REPO_SHORT)
    w('<nav class="tabbar" aria-label="区画">%s</nav>' % "".join(
        '<a href="#%s" data-nav>%s</a>' % (i, esc(s)) for i, s, _, _ in SECTIONS))
    w("<script>%s%s</script></body></html>" % (JS_COMMON, JS_SAVE if share else ""))
    return "".join(out)


# ---------------------------------------------------------------- 禁止語の照合

VOID = {"meta", "link", "br", "hr", "img", "input", "wbr", "source", "col", "area", "base", "embed", "param", "track"}
ATTR_KEEP = {"aria-label", "title", "alt", "content", "value", "placeholder", "data-label"}
SECTION_LABEL = {i: t for i, _, t, _ in SECTIONS}


class Visible(HTMLParser):
    """画面に出る文字（本文・SVG の文字・読み上げ用の属性）だけを、場所つきで集める。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self.stack = []
        self.skip = 0

    def _loc(self):
        for t in reversed(self.stack):
            if t[1]:
                return t[1]
        return "ページ"

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
        loc = a.get("data-field") or (SECTION_LABEL.get(a.get("id")) if tag == "section" else None)
        for k in ATTR_KEEP:
            if a.get(k):
                self.items.append((a[k], loc or self._loc()))
        if tag not in VOID:
            self.stack.append((tag, loc))

    def handle_startendtag(self, tag, attrs):
        a = dict(attrs)
        for k in ATTR_KEEP:
            if a.get(k):
                self.items.append((a[k], self._loc()))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.items.append((data, self._loc()))


def visible_items(html_text):
    p = Visible()
    p.feed(html_text)
    return p.items


def name_parts(name, generic, min_len=4):
    out = {name}
    for tok in re.split(r"[-_:/\s.]+", name):
        if len(tok) >= min_len and tok.lower() not in generic:
            out.add(tok)
    return out


def forbidden_terms(company, org, vocab):
    """{小文字の語: 種類}。自作スキル・自作サブエージェントの名前、パス・git のリモート・利用者の名前。"""
    generic = set(vocab["generic_words"])
    terms = {}
    people = org["people"]
    for s in list(company.get("skills", [])) + list(company.get("retired", [])):
        if s.get("public") or people.get(s["id"], {}).get("real_name_allowed"):
            continue
        for t in name_parts(s["id"], generic):
            terms.setdefault(t.lower(), "自作スキルの名前")
    for a in company.get("agents", []):
        if a.get("own"):
            for t in name_parts(a["id"], generic):
                terms.setdefault(t.lower(), "自作サブエージェントの名前")
    seeds = company.get("seeds", {})
    for key, kind in (("paths", "作業フォルダのパス"), ("remotes", "git のリモート名"), ("user", "利用者の名前")):
        for t in seeds.get(key, []):
            if len(t) >= 2:
                terms.setdefault(t.lower(), kind)
    return {t: k for t, k in terms.items() if len(t) >= 2}


def allowed_blob(company, vocab, org=None):
    parts = []
    for d in vocab["departments"]:
        parts += [d["name"], d["title"]]
    parts += [w["name"] for w in vocab["works"]] + [vocab["other_work"]["name"], "その他", DEFAULT_COMPANY]
    parts += vocab["builtin_agents"]
    parts += [s["id"] for s in company.get("skills", []) if s.get("public")]
    parts += [a["id"] for a in company.get("agents", []) if not a.get("own")]
    parts += [REPO_SHORT, SKILL_DOC_URL]
    m = build_model({"today": company["today"], "founded": company["today"], "range": {}, "skills": [], "agents": []},
                    normalize_org({}, {"skills": []}, vocab), vocab)
    svg, _ = build_svg(m)
    parts += [t for t, _ in visible_items(build_html(m, "share", svg))]
    return "\n".join(parts).lower()


def term_hits(text, term):
    if len(term) < 4:
        return re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(term), text) is not None
    return term in text


def scan_leaks(html_text, company, org, vocab):
    """[(種類, 語, 場所)]。語の照合は小文字。許可された名前は先に取り除く。"""
    terms = forbidden_terms(company, org, vocab)
    blob = allowed_blob(company, vocab)
    allowed_names = [s["id"] for s in company.get("skills", []) if org["people"].get(s["id"], {}).get("real_name_allowed")]
    allowed_names += list(org.get("allowed_terms", []))
    live = {t: k for t, k in terms.items() if t not in blob and t not in {x.lower() for x in org.get("allowed_terms", [])}}
    hits = []
    for text, loc in visible_items(html_text):
        low = text.lower()
        for n in sorted(allowed_names, key=len, reverse=True):
            low = low.replace(n.lower(), " ")
        for t, k in live.items():
            if term_hits(low, t):
                hits.append((k, t, loc))
    return hits


def share_strings(m):
    """外に出す版に載る文字列の一覧（本人の確認用）。"""
    emps = m["emps"]
    out = [("会社名", [m["name"]])]
    titles = sorted({e["share"] for e in emps if not e["public"] and e["share"] == e["title"]}, key=lambda t: (len(t), t))
    out.append(("仮の職名", titles))
    out.append(("公開スキルの実名", sorted({e["share"] for e in emps if e["public"]})))
    out.append(("実名にしたスキル", sorted({e["share"] for e in emps if not e["public"] and e["share"] != e["title"]})))
    out.append(("外部スタッフ", sorted({a["share"] for a in m["agents"]})))
    out.append(("部署名", [n for _, n, _ in m["dept_rows"]]))
    out.append(("求人票", ["%s／%s（この30日で%s%d回）" % (j["dept"], j["work"], j["prefix"], j["est"]) for j in m["jobs"]]))
    return [(k, v) for k, v in out if v]


def mask_company_name(name, terms):
    out = name
    for t in sorted(terms, key=len, reverse=True):
        out = re.sub(re.escape(t), "■" * min(len(t), 6), out, flags=re.I)
    return out


# ---------------------------------------------------------------- 実行

def history_row(company, org, m):
    emp = {e["id"]: [d2s(e["joined"]) or None, d2s(e["last_used"]) or None, e["n30"]] for e in m["emps"]}
    depts = {}
    for e in m["emps"]:
        depts[e["dept"]] = depts.get(e["dept"], 0) + 1
    rng = [company["range"]["oldest"], company["range"]["newest"]] if company["range"].get("oldest") else []
    return {"date": company["today"], "range": rng, "employees": emp, "employee_count": len(emp), "depts": depts}


def append_history(path, row):
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            rows = [ln for ln in f.read().split("\n") if ln.strip()]
    except OSError:
        pass
    keep = []
    for ln in rows:
        try:
            if json.loads(ln).get("date") == row["date"]:
                continue
        except ValueError:
            pass
        keep.append(ln)
    keep.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(keep) + "\n")
    os.replace(tmp, path)


def read_history(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("date"):
                    rows.append(r)
    except OSError:
        pass
    return sorted(rows, key=lambda r: r["date"])


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def open_hint(path):
    try:
        with open("/proc/version", encoding="utf-8") as f:
            if "microsoft" in f.read().lower():
                r = subprocess.run(["wslpath", "-w", path], capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return path


def approval_code(share_html):
    """外に出す版の中身の印。見せた一覧と書く中身が同じときだけ承認が通る。"""
    return hashlib.sha256(share_html.encode("utf-8")).hexdigest()[:12]


def remove_typed(work_dir):
    """打った文の抜き出しを残さない（手順3で消し忘れても、ここで消す）。"""
    try:
        os.remove(os.path.join(work_dir, "typed.txt"))
    except FileNotFoundError:
        pass


def main(argv=None):
    home = os.path.expanduser("~/.claude/ai-company")
    ap = argparse.ArgumentParser(description="portal.html と share.html を作る")
    ap.add_argument("--work-dir", default=os.path.join(home, "work"))
    ap.add_argument("--out-dir", default=home)
    ap.add_argument("--org", default=None, help="org.json（既定は out-dir の中）")
    ap.add_argument("--history", default=None, help="history.jsonl（既定は out-dir の中）")
    ap.add_argument("--approve", metavar="CODE", default=None,
                    help="本人が確認した。直前に出た APPROVE_CODE を渡すと share.html を書く")
    ap.add_argument("--no-share", action="store_true", help="手元の版だけ作る")
    ap.add_argument("--resolve", choices=["mask", "allow"], help="禁止語に当たったときの続け方")
    args = ap.parse_args(argv)
    if sys.version_info < (3, 9):
        print("Python 3.9 以上が必要です", file=sys.stderr)
        return 2
    vocab = load_vocab()
    os.makedirs(args.out_dir, exist_ok=True)
    org_path = args.org or os.path.join(args.out_dir, "org.json")
    hist_path = args.history or os.path.join(args.out_dir, "history.jsonl")
    company = load_json(os.path.join(args.work_dir, "company.json"), {})
    if not company.get("today"):
        print("STATUS: error\ncompany.json を読めません。先に collect.py を実行してください", file=sys.stderr)
        return 2
    misplaced = os.path.join(args.work_dir, "org.json")
    if not args.org and not os.path.exists(org_path) and os.path.exists(misplaced):
        # 手順3で work/ に書かれた org.json を黙って無視しない（2026-10-21 試しで会社名が消えた）
        print("注意: org.json が %s にありました。%s に移して使います" % (misplaced, org_path))
        os.replace(misplaced, org_path)
    elif os.path.exists(misplaced):
        print("注意: %s は読みません。会社名などは %s に書いてください" % (misplaced, org_path))
    org = normalize_org(load_json(org_path, {}), company, vocab, warn=lambda s: print("注意: " + s))
    rows = read_history(hist_path)

    share_path = os.path.join(args.out_dir, "share.html")
    portal_path = os.path.join(args.out_dir, "portal.html")
    if os.path.exists(share_path):
        os.remove(share_path)  # 前回の分は最初に消す（R2-8）

    remove_typed(args.work_dir)
    m = build_model(company, org, vocab, rows)
    write_atomic(portal_path, build_html(m, "local"))
    print("手元の版: %s" % portal_path)

    def finish():
        write_atomic(org_path, json.dumps(org, ensure_ascii=False, indent=1))
        append_history(hist_path, history_row(company, org, m))

    if args.no_share:
        finish()
        print("OPEN_PATH: %s" % open_hint(portal_path))
        print("STATUS: done-no-share")
        return 0

    def build_share():
        mm = build_model(company, org, vocab, rows)
        svg, bottom = build_svg(mm)
        return mm, build_html(mm, "share", svg), bottom

    m, share_html, _ = build_share()
    hits = scan_leaks(share_html, company, org, vocab)
    if hits and args.resolve == "allow":
        org["allowed_terms"] = sorted(set(org["allowed_terms"]) | {t for _, t, _ in hits})
        m, share_html, _ = build_share()
        hits = scan_leaks(share_html, company, org, vocab)
    elif hits and args.resolve == "mask":
        org["company_name"] = mask_company_name(org["company_name"], {t for _, t, loc in hits if loc == "会社名"})
        m, share_html, _ = build_share()
        hits = scan_leaks(share_html, company, org, vocab)
    if hits:
        # 語と場所は端末にだけ出す。ファイルには書かない（R2-6）
        for kind, term, loc in hits:
            print("LEAK: 種類=%s 場所=%s 語=%s" % (kind, loc, term))
        named = sorted({t for _, t, _ in hits if t.lower() in (org.get("company_name") or "").lower()})
        if named:
            print("ヒント: %s はあなたが付けた会社名にも入っています。手元の%sと同じ綴りのため止まりました。"
                  "会社名として出してよければ --resolve allow、隠したければ会社名を変えるか --resolve mask"
                  % (" / ".join(named), "・".join(sorted({k for k, t, _ in hits if t in named}))))
        print("share.html は書いていません。続け方: --resolve mask（伏せ字にして作り直す）／--resolve allow（この語を許可する）／やめる")
        write_atomic(org_path, json.dumps(org, ensure_ascii=False, indent=1))
        print("STATUS: leak")
        return 3
    code = approval_code(share_html)
    if args.approve != code:
        if args.approve:
            print("見せたあとで外に出す版の中身が変わりました。もう一度本人に見せてください")
        print("外に出す版に載る文字列（これ以外は載りません）:")
        for kind, vals in share_strings(m):
            print("  [%s] %s" % (kind, " / ".join(vals)))
        write_atomic(org_path, json.dumps(org, ensure_ascii=False, indent=1))
        print("APPROVE_CODE: %s" % code)
        print("STATUS: awaiting-approval")
        return 0
    write_atomic(share_path, share_html)
    finish()
    print("外に出す版: %s" % share_path)
    print("OPEN_PATH: %s" % open_hint(share_path))
    print("OPEN_PATH_LOCAL: %s" % open_hint(portal_path))
    print("STATUS: done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
