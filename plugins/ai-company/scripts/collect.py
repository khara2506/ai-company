#!/usr/bin/env python3
"""~/.claude と git を読んで数える（決まった処理。同じ記録からは同じ結果）。

書くもの（出力先 --out-dir、既定は ~/.claude/ai-company/work）:
  company.json  名前・回数・日付。人が打った文は入れない
  typed.txt     人が打った文の抜き出し（最大300件・3万字以内）。手順2が終わったら消す

どこにも送らない。外へ問い合わせない（git はこの PC の中の履歴を読むだけ）。
Python 3.9 以上・標準ライブラリだけ。
"""
import argparse
import getpass
import glob
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

import exclude as exclusion

HERE = os.path.dirname(os.path.abspath(__file__))
WINDOW_DAYS = 30
MAX_TYPED = 300
TYPED_CHARS_CAP = 30000
TYPED_ITEM_CHARS = 200
TYPED_MIN_CHARS = 8  # 「はい」「続けて」などの短い返事は作業の種類が分からないので数えない
EARLIEST_VALID = date(2023, 1, 1)  # Claude Code より前の日付は材料にしない

DATE_RE = re.compile(r'"timestamp"\s*:\s*"(\d{4}-\d{2}-\d{2})')
CWD_RE = re.compile(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')
SKILL_CALL_RE = re.compile(r'"name"\s*:\s*"(Skill|Agent|Task)"')
USER_RE = re.compile(r'"type"\s*:\s*"user"')
ASSIST_RE = re.compile(r'"type"\s*:\s*"assistant"')

# 人が打った文ではないタグ付きの文（origin の無い古い記録で使う）
NON_HUMAN_PREFIXES = (
    "<command-", "<local-command", "<task-notification", "<system-reminder",
    "<user-prompt-submit-hook", "<bash-", "Caveat:", "[Request interrupted",
    "This session is being continued from a previous conversation",  # 自動要約（印の無い古い形）
)

SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|pk|rk|ghp|gho|ghs|github_pat|xox[abp]|AKIA|AIza)[-_A-Za-z0-9]{12,}"),
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
    re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}"),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    re.compile(r"https?://[^\s/]+:[^\s/@]+@\S+"),
]


def load_vocab():
    with open(os.path.join(HERE, "vocab.json"), encoding="utf-8") as f:
        return json.load(f)


def eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def parse_date(s):
    """ISO 日付・日時の文字列から date（UTC の日付）。読めなければ None。"""
    if not s or not isinstance(s, str):
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def local_day(ts):
    """会話の記録の時刻（UTC の ISO 形式）を、手元の時刻帯の日付にする。読めなければ None。"""
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date()
    except ValueError:
        return parse_date(ts)


def valid_date(d, today):
    return d if d and EARLIEST_VALID <= d <= today else None


def parse_frontmatter(text):
    """SKILL.md の先頭の --- ... --- から name と description を取る（簡易）。"""
    out = {}
    if not text.startswith("---"):
        return out
    lines = text.split("\n")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return out
    i = 1
    while i < end:
        m = re.match(r"^([A-Za-z_-]+):\s*(.*)$", lines[i])
        if m:
            key, val = m.group(1), m.group(2).strip()
            if val in (">", "|", ">-", "|-", ">+", "|+"):
                buf = []
                i += 1
                while i < end and (lines[i].startswith(" ") or not lines[i].strip()):
                    buf.append(lines[i].strip())
                    i += 1
                val = " ".join(x for x in buf if x)
                i -= 1
            else:
                val = val.strip("\"'")
            out[key] = val
        i += 1
    return out


def git(args, cwd, timeout=10):
    try:
        # 会話に出てきたフォルダの .git/config で外のプログラムが動かないように止める
        safe = ["-c", "core.fsmonitor=false", "-c", "log.showSignature=false", "-c", "core.hooksPath=/dev/null"]
        r = subprocess.run(["git", "-C", cwd] + safe + args, capture_output=True, text=True,
                           timeout=timeout, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def file_birth(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    ts = getattr(st, "st_birthtime", None) or min(st.st_mtime, st.st_ctime)
    return datetime.fromtimestamp(ts).date()  # 手元の時刻帯


def first_commit_date(directory):
    """そのフォルダの git の初回コミット日。git 管理でなければ None。"""
    real = os.path.realpath(directory)
    out = git(["log", "--format=%aI", "--", "."], real)
    if not out or not out.strip():
        return None
    return parse_date(out.strip().split("\n")[-1])


GITHUB_URL_RE = re.compile(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


def github_repo(url):
    """GitHub の URL から owner/repo（小文字）。GitHub でなければ None。"""
    m = GITHUB_URL_RE.match((url or "").strip())
    return ("%s/%s" % m.groups()).lower() if m else None


def known_public_repos(vocab):
    """公開と確かめた配布元（owner/repo）。ここに無いものは、GitHub にあっても非公開かもしれないので伏せる。"""
    return {r.lower() for r in vocab["public_marketplace_repos"] + vocab.get("public_skill_repos", [])}


def skill_lock_source(real_dir):
    """`npx skills` で入れたもの（.agents/skills/<名前> に置かれる）なら、その配布元の記録。

    記録は、ホームに入れたときは ~/.agents/.skill-lock.json、プロジェクトに入れたときは <プロジェクト>/skills-lock.json。
    """
    skills_dir = os.path.dirname(real_dir)
    agents = os.path.dirname(skills_dir)
    if os.path.basename(skills_dir) != "skills" or os.path.basename(agents) != ".agents":
        return None
    for lock_path in (os.path.join(agents, ".skill-lock.json"),
                      os.path.join(os.path.dirname(agents), "skills-lock.json")):
        lock = read_json(lock_path, {})
        entries = lock.get("skills") if isinstance(lock, dict) else None
        entry = entries.get(os.path.basename(real_dir)) if isinstance(entries, dict) else None
        if isinstance(entry, dict):
            return entry
    return None


def repo_top(directory):
    out = git(["rev-parse", "--show-toplevel"], directory, timeout=5)
    return os.path.realpath(out.strip()) if out and out.strip() else None


def others_work(md, public_repos, home_repo=None, my_emails=frozenset()):
    """SKILL.md の実体が他人の作か。git の作者に自分（user.email）がいなければ他人の作。

    gstack などのスキル集は ~/.claude/skills に symlink や別の git リポジトリで入るので、
    実体の場所の履歴を見る（2026-10-09 の実地インストールで自作に数えていた）。
    戻り値: None（自作か分からない）または {"public": 公開と確かめた配布元か, "installed": 入れた日の材料}
    """
    real = os.path.realpath(md)
    real_dir = os.path.dirname(real)
    lock = skill_lock_source(real_dir)
    if lock:
        src = str(lock.get("source") or "").lower()
        return {"public": lock.get("sourceType") == "github" and src in public_repos,
                "installed": parse_date(lock.get("installedAt"))}
    # ~/.claude ごと git で管理している人の ~/.claude/skills の中身は、本人の持ち物として自作。
    # 作者のアドレスでは比べない（アドレスを変えた人の昔のスキルが他人の作になった。2026-10-21）。
    # プロジェクトの .claude/skills や、置き場所そのものに clone したスキル集には当てない（人のリポジトリかもしれない）
    if home_repo and repo_top(real_dir) == home_repo:
        return None
    authors = git(["log", "--format=%ae", "--", os.path.basename(real)], real_dir)
    if not authors or not authors.strip():
        return None
    me = {e.strip().lower() for e in (git(["config", "--get-all", "user.email"], real_dir) or "").split("\n") if e.strip()}
    me |= my_emails
    if not me or me & {a.strip().lower() for a in authors.split("\n")}:
        return None  # 自分の名前が分からなければ自作のまま（伏せる側に倒す）
    origin = (git(["remote", "get-url", "origin"], real_dir, timeout=5) or "").strip()
    public = github_repo(origin) in public_repos and in_upstream(real_dir, os.path.basename(real))
    return {"public": public, "installed": None}


def in_upstream(real_dir, name):
    """配布元の追跡ブランチ（origin/HEAD か、今のブランチの上流）にそのファイルがあるか。

    公開のスキル集を clone して自分のスキルを足した場合、そのスキルは配布元に無いので公開扱いにしない。
    """
    prefix = (git(["rev-parse", "--show-prefix"], real_dir) or "").strip()
    for ref in ("refs/remotes/origin/HEAD", "@{upstream}"):
        if git(["cat-file", "-e", "%s:%s%s" % (ref, prefix, name)], real_dir) is not None:
            return True
    return False


def entry_birth(path):
    """symlink を辿らずに、置いた日（入れた日）を取る。"""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    ts = getattr(st, "st_birthtime", None) or min(st.st_mtime, st.st_ctime)
    return datetime.fromtimestamp(ts).date()  # 手元の時刻帯


DELEGATE_RE = re.compile(r"(subagent|サブエージェント|Agent\s*ツール|Agent tool|Task tool|subagent_type|Skill\s*ツール|Skill tool)", re.I)


def mentions_delegation(text):
    """手順書に、サブエージェントやほかのスキルを使う書き方があるか（まとめ役の見分けの片方）。"""
    return bool(DELEGATE_RE.search(text or ""))


def home_config_repo(root):
    """~/.claude/skills が、その上（~/.claude かそれより上）を一番上とする git リポジトリに入っていれば、その一番上。"""
    top = repo_top(root)
    real_root = os.path.realpath(root)
    return top if top and top != real_root and real_root.startswith(top + os.sep) else None


def my_past_emails(claude_dir):
    """~/.claude を git で管理していれば、そこで本人がコミットに使ってきたアドレス（アドレスを変えた人の昔のスキルのため）。"""
    root = os.path.join(claude_dir, "skills")
    repo = home_config_repo(root) if os.path.isdir(root) else None
    if not repo:
        return frozenset()
    out = git(["log", "-n", "2000", "--format=%ae"], repo) or ""
    return frozenset(e.strip().lower() for e in out.split("\n") if e.strip())


def discover_skill_dir(root, today, public_repos, user_level=False, my_emails=frozenset()):
    """root（…/.claude/skills）の中のスキル。他人の作は kind=external。

    公開扱い（実名を外に出す版に載せてよい）にするのは、配布元が vocab.json の公開の一覧にあるものだけ。
    GitHub にあっても非公開のリポジトリ（チームや社内のスキル集）かもしれないので、一覧に無ければ伏せる。
    """
    skills = []
    if not os.path.isdir(root):
        return skills
    home_repo = home_config_repo(root) if user_level else None
    for entry in sorted(os.listdir(root)):
        d = os.path.join(root, entry)
        md = os.path.join(d, "SKILL.md")
        if not os.path.isfile(md):
            continue
        try:
            with open(md, encoding="utf-8", errors="replace") as f:
                body = f.read(200000)
        except OSError:
            body = ""
        fm = parse_frontmatter(body[:20000])
        ext = others_work(md, public_repos, home_repo, my_emails)
        if ext:
            material = valid_date(ext["installed"], today) or valid_date(entry_birth(d), today)
        else:
            material = valid_date(first_commit_date(d), today) or valid_date(file_birth(md), today)
        skills.append({
            "id": fm.get("name") or entry, "alias": entry, "kind": "external" if ext else "own",
            "public": bool(ext and ext["public"]),
            "plugin": None, "description": fm.get("description", ""),
            "material_date": material.isoformat() if material else None,
            "delegates_in_text": mentions_delegation(body),
        })
    return skills


def discover_own_skills(claude_dir, today, vocab):
    return discover_skill_dir(os.path.join(claude_dir, "skills"), today, known_public_repos(vocab), user_level=True)


def is_user_config(dot_claude):
    """ホームの ~/.claude（会話の記録 projects/ がある）か。プロジェクトの .claude には projects/ が無い。

    --claude-dir で別の場所を読んでいるとき、さかのぼった先の本物の ~/.claude/skills を
    プロジェクトのスキルとして数えないため。
    """
    return os.path.isdir(os.path.join(dot_claude, "projects"))


def project_skill_roots(cwds, claude_dir, skip=None):
    """作業したフォルダとその上にある .claude/skills（Claude Code がプロジェクトのスキルとして読む場所）。読まないフォルダの下は除く。"""
    user_root = os.path.realpath(os.path.join(claude_dir, "skills"))
    roots, seen = [], {user_root}
    for c in sorted(cwds):
        d = c
        while d and os.path.isabs(d):
            cand = os.path.join(d, ".claude", "skills")
            if (os.path.isdir(cand) and os.sep + os.path.join(".claude", "worktrees") + os.sep not in cand
                    and not is_user_config(os.path.dirname(cand)) and not (skip and skip.covers(cand))):
                real = os.path.realpath(cand)
                if real not in seen:
                    seen.add(real)
                    roots.append(cand)
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    return roots


def discover_project_skills(cwds, claude_dir, today, vocab, skip=None):
    skills = []
    for root in project_skill_roots(cwds, claude_dir, skip):
        skills.extend(discover_skill_dir(root, today, known_public_repos(vocab), my_emails=my_past_emails(claude_dir)))
    return skills


def own_plugin_name():
    """このプラグイン自身の名前（自分は社員に数えない）。"""
    pj = read_json(os.path.join(os.path.dirname(HERE), ".claude-plugin", "plugin.json"), {})
    return pj.get("name") if isinstance(pj, dict) else None


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def marketplace_is_public(mp, known, vocab):
    if mp in vocab["public_marketplace_names"]:
        return True
    src = (known.get(mp) or {}).get("source") or {}
    return src.get("source") == "github" and src.get("repo") in vocab["public_marketplace_repos"]


def discover_plugin_skills(claude_dir, today, vocab):
    skills, plugin_info = [], {}
    me = own_plugin_name()
    installed = read_json(os.path.join(claude_dir, "plugins", "installed_plugins.json"), {})
    known = read_json(os.path.join(claude_dir, "plugins", "known_marketplaces.json"), {})
    for key, installs in sorted((installed.get("plugins") or {}).items()):
        plugin, _, mp = key.partition("@")
        if not installs or plugin == me:
            continue
        inst = sorted(installs, key=lambda x: x.get("installedAt") or "")[0]
        public = marketplace_is_public(mp, known, vocab)
        installed_at = valid_date(parse_date(inst.get("installedAt")), today)
        plugin_info[plugin] = {"public": public, "installed_at": installed_at}
        base = inst.get("installPath") or ""
        for md in sorted(glob.glob(os.path.join(base, "skills", "*", "SKILL.md"))):
            entry = os.path.basename(os.path.dirname(md))
            try:
                with open(md, encoding="utf-8", errors="replace") as f:
                    body = f.read(200000)
            except OSError:
                body = ""
            fm = parse_frontmatter(body[:20000])
            skills.append({
                "id": "%s:%s" % (plugin, fm.get("name") or entry), "alias": entry, "kind": "plugin",
                "public": public, "plugin": plugin, "description": fm.get("description", ""),
                "material_date": installed_at.isoformat() if installed_at else None,
                "delegates_in_text": mentions_delegation(body),
            })
    return skills, plugin_info


def clean_typed(text):
    t = re.sub(r"\s+", " ", text).strip()
    for rx in SECRET_PATTERNS:
        t = rx.sub("■", t)
    return t[:TYPED_ITEM_CHARS]


def human_text(d):
    """人が打った文なら本文、そうでなければ None（設計書 R2-3）。"""
    if d.get("type") != "user" or d.get("isSidechain") or d.get("isMeta") or d.get("toolUseResult"):
        return None
    if d.get("isCompactSummary") or d.get("isVisibleInTranscriptOnly"):  # 会話の自動要約
        return None
    if d.get("entrypoint") == "sdk-cli":  # 定時処理などの自動実行（E-2）
        return None
    msg = d.get("message")
    if not isinstance(msg, dict) or msg.get("role", "user") != "user":
        return None
    content = msg.get("content")
    if isinstance(content, list):
        if any(not isinstance(b, dict) or b.get("type") != "text" for b in content):
            return None  # tool_result などが混ざる行
        content = "\n".join(b.get("text", "") for b in content)
    if not isinstance(content, str):
        return None
    stripped = content.lstrip()
    if stripped.startswith(NON_HUMAN_PREFIXES):
        return None
    origin = d.get("origin")
    if isinstance(origin, dict):
        if origin.get("kind") != "human":
            return None
    else:  # origin の無い古い形
        if d.get("entrypoint", "cli") != "cli" or stripped.startswith("<"):
            return None
    return content


def is_turn_start(d):
    """人が何か送った行か（区切り）。human_text より広く、画像つき・origin の無い古い形・VS Code からの文も含む。"""
    if d.get("type") != "user" or d.get("isSidechain") or d.get("isMeta") or d.get("toolUseResult"):
        return False
    if d.get("isCompactSummary") or d.get("entrypoint") == "sdk-cli":
        return False
    msg = d.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return False
        content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return isinstance(content, str) and not content.lstrip().startswith(NON_HUMAN_PREFIXES)


def slash_command(d):
    """利用者が打った /名前 なら名前。"""
    if d.get("type") != "user" or d.get("isSidechain") or d.get("isMeta"):
        return None
    msg = d.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(content, str):
        return None
    m = re.match(r"\s*<command-name>\s*/?([^<\s]+)\s*</command-name>", content)
    return m.group(1) if m else None


def project_files(claude_dir):
    out = []
    for p in glob.glob(os.path.join(claude_dir, "projects", "*", "*.jsonl")):
        if os.sep + "subagents" + os.sep in p:
            continue
        out.append(p)
    return sorted(out)


def read_transcripts(claude_dir, today, log=eprint, skip=None):
    """会話の記録を1回なめて、必要なものだけ取り出す。

    skip（読まない作業フォルダ）のフォルダの記録は開かない。ほかの記録でも、作業フォルダが skip の下にある行は数えない
    （会話の途中で cd して入った分。cwd の無い行は、同じファイルで最後に見た cwd で決める）。
    """
    files = project_files(claude_dir)
    if skip:
        kept, dropped = [], set()
        for p in files:
            folder = os.path.basename(os.path.dirname(p))
            if skip.covers_folder(folder):
                dropped.add(folder)
                skip.files += 1
            else:
                kept.append(p)
        files, skip.folders = kept, len(dropped)
    total_bytes = sum(os.path.getsize(p) for p in files if os.path.exists(p))
    if files:
        log("会話の記録を読んでいます（%d ファイル・約 %s）" % (len(files), human_size(total_bytes)))
    win_start = today - timedelta(days=WINDOW_DAYS - 1)
    res = {
        "files": len(files), "lines": 0, "bad_lines": 0, "oldest": None, "newest": None,
        "calls": [],     # (date, kind, name)  kind: skill | slash | agent
        "typed": [],     # (date, ts, text)
        "cwds": set(),
        "delegations": {},  # 呼ばれたスキルの名前 -> そのあと人が打つまでに頼んだ回数
    }
    seen = set()
    for i, path in enumerate(files):
        active = None  # いま動いているスキル（人が打つと終わる）
        last_cwd = None
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                res["lines"] += 1
                if not line.endswith("}"):
                    res["bad_lines"] += 1
                    continue
                if skip:
                    m = CWD_RE.search(line)
                    if m:
                        try:
                            last_cwd = json.loads('"%s"' % m.group(1))
                        except ValueError:
                            last_cwd = m.group(1)
                    if last_cwd and skip.covers(last_cwd):
                        skip.lines += 1
                        continue
                m = DATE_RE.search(line)
                day = parse_date(m.group(1)) if m else None
                if day and day <= today + timedelta(days=1):
                    if res["oldest"] is None or day < res["oldest"]:
                        res["oldest"] = day
                    if res["newest"] is None or day > res["newest"]:
                        res["newest"] = day
                is_user = USER_RE.search(line) is not None
                is_assist = (not is_user) and ASSIST_RE.search(line) is not None
                if is_user:
                    if '"tool_use_id"' in line or '"toolUseResult"' in line:
                        continue
                elif is_assist:
                    if not SKILL_CALL_RE.search(line):
                        continue
                else:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    res["bad_lines"] += 1
                    continue
                if not isinstance(d, dict) or "type" not in d or "message" not in d:
                    continue
                ts = d.get("timestamp")
                day = local_day(ts)
                if day is None:
                    continue
                cwd = d.get("cwd")
                if skip and isinstance(cwd, str) and skip.covers(cwd):
                    skip.lines += 1
                    continue
                if isinstance(cwd, str) and len(res["cwds"]) < 5000:
                    res["cwds"].add(cwd)
                if is_assist:
                    msg = d.get("message")
                    content = msg.get("content") if isinstance(msg, dict) else None
                    if not isinstance(content, list) or d.get("isSidechain"):
                        continue
                    for b in content:
                        if not isinstance(b, dict) or b.get("type") != "tool_use":
                            continue
                        bid = b.get("id")
                        if bid:
                            if bid in seen:
                                continue
                            seen.add(bid)
                        inp = b.get("input") or {}
                        if b.get("name") == "Skill" and isinstance(inp.get("skill"), str):
                            res["calls"].append((day, "skill", inp["skill"]))
                            if active is not None and active != inp["skill"]:
                                # 動いているスキルが別のスキルを呼んだ: 頼み1回。そのあとの頼みは呼ばれた側に付ける
                                res["delegations"][active] = res["delegations"].get(active, 0) + 1
                            active = inp["skill"]
                        elif b.get("name") in ("Agent", "Task"):
                            res["calls"].append((day, "agent", inp.get("subagent_type") or "general-purpose"))
                            if active is not None:
                                res["delegations"][active] = res["delegations"].get(active, 0) + 1
                    continue
                uid = d.get("uuid")
                if uid:
                    if uid in seen:
                        continue
                    seen.add(uid)
                cmd = slash_command(d)
                if cmd:
                    res["calls"].append((day, "slash", cmd))
                    active = cmd
                    continue
                text = human_text(d)
                if text is not None or is_turn_start(d):
                    active = None
                if text is not None and day >= win_start:
                    t = clean_typed(text)
                    if len(t) >= TYPED_MIN_CHARS:
                        res["typed"].append((day, ts, t))
    return res


def human_size(n):
    if n >= 1e9:
        return "%.1f GB" % (n / 1e9)
    if n >= 1e6:
        return "%.0f MB" % (n / 1e6)
    return "%.0f KB" % (n / 1e3)


def select_even(items, limit):
    """日ごとにまとめ、30日に均等に散らして limit 件を選ぶ（R2-1）。小さい日は全部取り、余りを他へ回す。"""
    if len(items) <= limit:
        return list(items)
    by_day = {}
    for it in items:
        by_day.setdefault(it[0], []).append(it)
    days = sorted(by_day, key=lambda k: (len(by_day[k]), k))
    remaining, picked = limit, []
    for n_left, day in zip(range(len(days), 0, -1), days):
        lst = sorted(by_day[day], key=lambda x: x[1])
        quota = remaining // n_left
        k = min(len(lst), quota)
        if k <= 0:
            continue
        picked.extend(lst[int((j + 0.5) * len(lst) / k)] for j in range(k))
        remaining -= k
    return sorted(picked, key=lambda x: x[1])


def unique_typed(typed):
    """同じ本文は最初の1件だけ（言い直し・再送で同じ文が並ぶ。求人票の材料には1件で足りる）。"""
    seen, out = set(), []
    for it in sorted(typed, key=lambda x: x[1]):
        if it[2] not in seen:
            seen.add(it[2])
            out.append(it)
    return out


def choose_typed(typed, max_items=MAX_TYPED, char_cap=TYPED_CHARS_CAP):
    limit = max_items
    while True:
        sel = select_even(typed, limit)
        if sum(len(t[2]) for t in sel) <= char_cap or len(sel) <= 1:
            return sel
        limit = max(1, int(len(sel) * 0.9))


def history_rows(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("date"):
                    rows.append(r)
    except OSError:
        pass
    rows.sort(key=lambda r: r["date"])
    return rows


def retired_from_history(rows, current_ids, today):
    """前の実行にいて今いないスキルは「退職」。退職日は、いなくなったと分かった日。"""
    present, retired, info = set(), {}, {}
    seq = [(r["date"], set((r.get("employees") or {}).keys()), r.get("employees") or {}) for r in rows]
    seq.append((today.isoformat(), set(current_ids), {}))
    for dt, ids, emp in seq:
        for sid in present - ids:
            if sid not in retired:
                j, lu = (info.get(sid) or [None, None, 0])[:2]
                retired[sid] = {"id": sid, "joined": j, "last_used": lu, "retired_on": dt}
        for sid in ids:
            retired.pop(sid, None)
        for sid, v in emp.items():
            info[sid] = v
        present = ids
    return [retired[k] for k in sorted(retired)]


def name_parts(name, min_len):
    out = {name}
    for tok in re.split(r"[-_:/\s.]+", name):
        if len(tok) >= min_len:
            out.add(tok)
    return out


def path_tokens(path, generic):
    out = set()
    for part in re.split(r"[\\/]+", path):
        if not part or part.startswith("."):
            continue
        for cand in [part] + re.split(r"[-_.\s]+", part):
            if len(cand) >= 4 and cand.lower() not in generic and not re.fullmatch(r"[\d\-_.:/]+", cand):
                out.add(cand)
    return out


REMOTE_STOP = {"github", "gitlab", "bitbucket", "https", "http", "ssh", "git", "com", "org", "net", "www"}


def collect_seeds(cwds, generic, claude_dir):
    """外に出す版の照合で使う、手元だけの固有の語（パスの部分・git のリモート名・利用者の名前）。"""
    generic = set(generic)
    home = os.path.expanduser("~")
    paths, remotes, user = set(), set(), set()
    for c in cwds:
        paths |= path_tokens(c, generic)
    names = {getpass.getuser() if _can_getuser() else "", os.path.basename(home.rstrip("/"))}
    gn = git(["config", "--global", "user.name"], os.getcwd())
    if gn:
        names |= {gn.strip()} | set(re.split(r"\s+", gn.strip()))
    for n in names:
        if len(n) >= 3 and n.lower() not in generic:
            user.add(n)
    seen_roots = set()
    for c in sorted(cwds)[:400]:
        if len(seen_roots) >= 150 or not os.path.isdir(c):
            continue
        top = git(["rev-parse", "--show-toplevel"], c, timeout=5)
        if not top or top.strip() in seen_roots:
            continue
        seen_roots.add(top.strip())
        out = git(["remote", "-v"], top.strip(), timeout=5)
        for tok in re.split(r"[\s/:@.]+", out or ""):
            if len(tok) >= 4 and tok.lower() not in REMOTE_STOP | generic and tok not in ("fetch)", "(fetch)", "(push)", "origin"):
                remotes.add(tok)
    remotes -= {"(fetch)", "(push)", "origin"}
    return {"paths": sorted(paths), "remotes": sorted(remotes), "user": sorted(user)}


def _can_getuser():
    try:
        getpass.getuser()
        return True
    except Exception:  # noqa: BLE001  環境によって例外の種類が違う
        return False


def is_builtin_skill(name, vocab):
    """Claude Code に最初から入っているスキル（ディスクに SKILL.md が無い）か。"""
    return name in vocab["builtin_skills"] or name.startswith(tuple(vocab["builtin_skill_prefixes"]))


MANAGER_MIN_RUNS = 2


def is_manager(skill, runs, delegations):
    """まとめ役（ほかのスキルやサブエージェントに仕事を割り振るスキル）か。

    手順書の書き方だけでも、会話の記録だけでも外れが多い（前置きに「サブエージェント」と書くスキル集、
    参照用のスキルを読んだあとの作業）ので、両方がそろったときだけ。記録では、呼ばれるたびに平均1回以上頼んでいること。
    """
    return bool(skill.get("delegates_in_text")) and runs >= MANAGER_MIN_RUNS and delegations >= runs


def resolve_skill(name, by_id, by_bare):
    if name in by_id:
        return by_id[name]
    if name in by_bare and len(by_bare[name]) == 1:
        return by_bare[name][0]
    return None


def collect(claude_dir, out_dir, today, history_path=None, log=eprint, vocab=None, skip=None):
    vocab = vocab or load_vocab()
    skip = skip or exclusion.Excluder()
    tr = read_transcripts(claude_dir, today, log=log, skip=skip)
    own = discover_own_skills(claude_dir, today, vocab)
    proj = discover_project_skills(tr["cwds"], claude_dir, today, vocab, skip)
    plug, plugin_info = discover_plugin_skills(claude_dir, today, vocab)
    skills = own + proj + plug
    # 同じ id が重なったら先のものだけ
    uniq, ids = [], set()
    for s in skills:
        if s["id"] not in ids:
            ids.add(s["id"])
            uniq.append(s)
    skills = uniq
    by_id = {s["id"]: s for s in skills}
    by_bare = {}
    for s in skills:
        by_bare.setdefault(s["alias"], []).append(s)
        if s["kind"] != "plugin" and s["alias"] != s["id"]:
            by_id.setdefault(s["alias"], s)

    win_start = today - timedelta(days=WINDOW_DAYS - 1)
    me = own_plugin_name()
    me_calls = {me, "%s:%s" % (me, me)} - set(by_id) if me else set()  # 同じ名前の自作スキルがあればそちらに数える

    stats = {s["id"]: {"first": None, "last": None, "n30": 0, "total": 0} for s in skills}
    agents = {}
    unmatched = 0
    for day, kind, name in tr["calls"]:
        if kind == "agent":
            a = agents.setdefault(name, {"first": None, "last": None, "n30": 0, "total": 0})
            tgt = a
        else:
            if name in me_calls:
                continue
            s = resolve_skill(name, by_id, by_bare)
            if s is None and is_builtin_skill(name, vocab):
                # Claude Code に最初から入っているスキルは、組み込みのサブエージェントと同じく外部スタッフ
                tgt = agents.setdefault("/" + name, {"first": None, "last": None, "n30": 0, "total": 0})
            elif s is None:
                unmatched += 1
                continue
            else:
                tgt = stats[s["id"]]
        tgt["total"] += 1
        if day >= win_start:
            tgt["n30"] += 1
        if tgt["first"] is None or day < tgt["first"]:
            tgt["first"] = day
        if tgt["last"] is None or day > tgt["last"]:
            tgt["last"] = day

    rows = history_rows(history_path) if history_path else []
    hist_emp = {}
    for r in rows:
        for sid, v in (r.get("employees") or {}).items():
            hist_emp[sid] = v

    deleg = {}
    for name, n in tr["delegations"].items():
        s = resolve_skill(name, by_id, by_bare)
        if s is not None:
            deleg[s["id"]] = deleg.get(s["id"], 0) + n

    roster = []
    for s in skills:
        st = stats[s["id"]]
        h = hist_emp.get(s["id"]) or [None, None, 0]
        material = parse_date(s["material_date"])
        cands = [d for d in (material, st["first"], parse_date(h[0])) if d]
        joined = min(cands) if cands else None
        lasts = [d for d in (st["last"], parse_date(h[1])) if d]
        last = max(lasts) if lasts else None
        roster.append({
            "id": s["id"], "kind": s["kind"], "public": s["public"], "plugin": s["plugin"],
            "description": s["description"],
            "joined": joined.isoformat() if joined else None,
            "first_used": st["first"].isoformat() if st["first"] else None,
            "last_used": last.isoformat() if last else None,
            "count_30d": st["n30"], "count_total": st["total"],
            "manager": is_manager(s, st["total"], deleg.get(s["id"], 0)),
        })

    agent_list = []
    for name, a in sorted(agents.items()):
        if name.startswith("/"):  # 組み込みのスキル
            builtin, public = True, True
        elif ":" in name:
            plugin = name.split(":", 1)[0]
            builtin, public = False, bool(plugin_info.get(plugin, {}).get("public"))
        else:
            builtin = name in vocab["builtin_agents"]
            public = builtin
        agent_list.append({
            "id": name, "builtin": builtin, "public": public or builtin, "own": not (builtin or public),
            "first_used": a["first"].isoformat(), "last_used": a["last"].isoformat(),
            "count_30d": a["n30"], "count_total": a["total"],
        })

    selected = choose_typed(unique_typed(tr["typed"]))
    typed_lines = ["%d\t%s\t%s" % (i + 1, t[0].isoformat(), t[2]) for i, t in enumerate(selected)]

    settings = read_json(os.path.join(claude_dir, "settings.json"), {})
    retention = settings.get("cleanupPeriodDays") if isinstance(settings, dict) else None

    oldest = tr["oldest"]
    hist_first = parse_date(rows[0]["date"]) if rows else None
    skill_joined = [parse_date(r["joined"]) for r in roster if r["joined"]]
    founded_c = [d for d in [oldest, hist_first] + skill_joined if d]
    founded = min(founded_c) if founded_c else today

    # 読まないフォルダの下のスキルは、退職としても出さない。同じ名前の社員が外にいれば、その人は残す
    gone = (skip.skill_names() if skip else set()) - {r["id"] for r in roster}
    hist_ranges = [parse_date(x) for r in rows for x in (r.get("range") or []) if parse_date(x)]
    seeds = collect_seeds(tr["cwds"], vocab["generic_words"], claude_dir)

    company = {
        "schema": 1,
        "today": today.isoformat(),
        "founded": founded.isoformat(),
        "range": {"oldest": oldest.isoformat() if oldest else None,
                  "newest": tr["newest"].isoformat() if tr["newest"] else None},
        "history_range": {"oldest": min(hist_ranges).isoformat() if hist_ranges else None,
                          "rows": len(rows)},
        "retention_days": retention if isinstance(retention, int) else None,
        "stats": {"files": tr["files"], "lines": tr["lines"], "bad_lines": tr["bad_lines"],
                  "unmatched_calls": unmatched},
        "skills": roster,
        "agents": agent_list,
        "retired": [r for r in retired_from_history(rows, [r["id"] for r in roster], today) if r["id"] not in gone],
        "typed": {"total_30d": len(tr["typed"]), "sample_n": len(selected),
                  "chars": sum(len(t[2]) for t in selected), "found": bool(tr["typed"]),
                  "has_transcripts": tr["files"] > 0},
        "seeds": seeds,
        "exclude": {"dirs": len(skip.dirs), "folders": skip.folders, "files": skip.files, "lines": skip.lines,
                    "drop": sorted(exclusion.name_hash(n) for n in gone)},
    }
    os.makedirs(out_dir, exist_ok=True)
    os.chmod(out_dir, 0o700)  # 打った文の抜き出しが入るので本人だけ
    with open(os.path.join(out_dir, "company.json"), "w", encoding="utf-8") as f:
        json.dump(company, f, ensure_ascii=False, indent=1)
    typed_path = os.path.join(out_dir, "typed.txt")
    fd = os.open(typed_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(typed_path, 0o600)  # 前からあったファイルも本人だけに
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(typed_lines) + ("\n" if typed_lines else ""))
    return company


def summary(c):
    s = c["stats"]
    lines = [
        "スキル %d（自作 %d・入れたスキル集 %d・プラグイン %d）／サブエージェントの種類 %d／退職 %d" % (
            len(c["skills"]), sum(1 for x in c["skills"] if x["kind"] == "own"),
            sum(1 for x in c["skills"] if x["kind"] == "external"),
            sum(1 for x in c["skills"] if x["kind"] == "plugin"), len(c["agents"]), len(c["retired"])),
        "記録 %d ファイル・%d 行（読めなかった行 %d）／範囲 %s 〜 %s" % (
            s["files"], s["lines"], s["bad_lines"], c["range"]["oldest"], c["range"]["newest"]),
        "人が打った文（この30日）%d 件、抜き出し %d 件（%d 字。同じ文は1件にまとめ、多いときは日ごとに均して選ぶ）" % (
            c["typed"]["total_30d"], c["typed"]["sample_n"], c["typed"]["chars"]),
        "創業日 %s" % c["founded"],
    ]
    if c["typed"]["has_transcripts"] and not c["typed"]["found"]:
        lines.append("人が打った文を見つけられませんでした（記録の形が変わった可能性）")
    if c["retention_days"] is not None:
        lines.append("記録の保持日数 %s 日" % c["retention_days"])
    ex = c.get("exclude") or {}
    if ex.get("dirs"):
        lines.append("読まない作業フォルダ %d か所: 記録のフォルダ %d・ファイル %d・行 %d を外しました" % (
            ex["dirs"], ex["folders"], ex["files"], ex["lines"]))
        if not (ex["folders"] or ex["lines"]):
            lines.append("注意: 読まない作業フォルダの指定が、どの記録にも当たっていません（打ち間違いでないか確かめてください）")
        elif ex["files"] and not s["files"] and not ex["lines"]:
            lines.append("注意: すべての記録を外しました")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="~/.claude を読んで数える")
    home = os.path.expanduser("~/.claude")
    ap.add_argument("--claude-dir", default=home)
    ap.add_argument("--out-dir", default=os.path.join(home, "ai-company", "work"))
    ap.add_argument("--history", default=None, help="history.jsonl（既定は out-dir の1つ上）")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD（テスト用）")
    ap.add_argument("--exclude", action="append", default=[], metavar="DIR",
                    help="読まない作業フォルダ（くり返し可）。既定で out-dir の1つ上の exclude.json も読む")
    args = ap.parse_args(argv)
    if sys.version_info < (3, 9):
        eprint("Python 3.9 以上が必要です")
        return 2
    today = date.fromisoformat(args.today) if args.today else date.today()  # 手元の時計の日付（日本で朝9時前でも前日にしない）
    base = os.path.dirname(os.path.abspath(args.out_dir))
    stale = os.path.join(args.out_dir, "typed.txt")
    if os.path.exists(stale):
        os.remove(stale)  # 前の実行の抜き出しを、止まったときにも残さない
    history = args.history or os.path.join(base, "history.jsonl")
    try:
        skip = exclusion.load(os.path.join(base, exclusion.FILE_NAME), args.exclude)
    except exclusion.ExcludeError as e:
        eprint("STATUS: error\n%s。読まないフォルダが分からないので、何も読まずに止まりました" % e)
        return 2
    for d in skip.missing():
        eprint("注意: 読まない作業フォルダが見つかりません: %s" % d)
    c = collect(args.claude_dir, args.out_dir, today, history_path=history, skip=skip)
    print(summary(c))
    if skip:
        print("読まない作業フォルダ: %s" % " ".join(skip.dirs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
