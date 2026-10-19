"""架空の ~/.claude を作る（実在の人・会社・パスは出てこない）。

  python3 tests/fakes.py <出力先> [tiny|small|medium|large] [new|old] [--leaky]

tiny=0人 / small=3人 / medium=約30人 / large=約300人。
new=今の形の会話の記録（origin あり）/ old=origin の無い古い形。
--leaky=顧客名（globex）をスキル名・説明文・作業フォルダ・git のリモートに入れる（漏れのテスト用）。
"""
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

TODAY = date(2026, 10, 8)
CUSTOMER = "globex"  # 漏れのテストで探す架空の顧客名

SIZES = {"tiny": 0, "small": 3, "medium": 30, "large": 300}
BASE_NAMES = ["invoice-maker", "weekly-report", "tweet-draft", "meeting-notes", "expense-sorter",
              "bug-hunter", "deploy-helper", "proofreader", "translator", "slide-maker"]

TYPED_SAMPLES = ["請求書を作って", "今週のレポートをまとめて", "このバグを直して", "議事録を整理して", "ツイートの案を3つ"]


def ts(day, hh=9, mm=0, ss=0):
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def jl(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class FakeHome:
    def __init__(self, root, form="new"):
        self.root = str(root)
        self.claude = os.path.join(self.root, ".claude")
        os.makedirs(os.path.join(self.claude, "skills"), exist_ok=True)
        os.makedirs(os.path.join(self.claude, "projects"), exist_ok=True)
        os.makedirs(os.path.join(self.claude, "plugins"), exist_ok=True)
        self.form = form
        self.plugins = {}
        self.known = {}
        self.n = 0

    # ---- スキル・プラグイン ----
    def add_skill(self, name, description="定型の作業をする"):
        d = os.path.join(self.claude, "skills", name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: %s\ndescription: %s\n---\n\n# %s\n" % (name, description, name))
        return d

    def add_plugin(self, plugin, marketplace, skills, installed_at="2026-09-01T00:00:00.000Z", repo=None):
        base = os.path.join(self.claude, "plugins", "cache", marketplace, plugin, "abc123")
        for s in skills:
            d = os.path.join(base, "skills", s)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write("---\nname: %s\ndescription: plugin skill\n---\n" % s)
        self.plugins["%s@%s" % (plugin, marketplace)] = [{
            "scope": "user", "installPath": base, "version": "abc123",
            "installedAt": installed_at, "lastUpdated": installed_at}]
        if repo:
            self.known[marketplace] = {"source": {"source": "github", "repo": repo}}
        else:
            self.known[marketplace] = {"source": {"source": "directory", "path": "/somewhere"}}

    def finish_plugins(self):
        with open(os.path.join(self.claude, "plugins", "installed_plugins.json"), "w") as f:
            json.dump({"version": 2, "plugins": self.plugins}, f)
        with open(os.path.join(self.claude, "plugins", "known_marketplaces.json"), "w") as f:
            json.dump(self.known, f)

    def git_commit_claude(self, when="2026-06-19T10:00:00+00:00"):
        env = dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when,
                   GIT_AUTHOR_NAME="Tester", GIT_AUTHOR_EMAIL="t@example.invalid",
                   GIT_COMMITTER_NAME="Tester", GIT_COMMITTER_EMAIL="t@example.invalid")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.claude, check=True, env=env)
        subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=self.claude, check=True)
        subprocess.run(["git", "add", "skills"], cwd=self.claude, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.claude, check=True, env=env)

    def add_collection(self, name, skills, author="other@example.invalid", remote="https://github.com/someone/%s.git"):
        """他人のスキル集を gstack と同じ形で入れる: 別の git リポジトリを skills/<name> に置き、
        各スキルは skills/<s>/SKILL.md をその中への symlink にする。"""
        repo = os.path.join(self.claude, "skills", name)
        env = dict(os.environ, GIT_AUTHOR_NAME="Other", GIT_AUTHOR_EMAIL=author,
                   GIT_COMMITTER_NAME="Other", GIT_COMMITTER_EMAIL=author)
        for s in [name] + list(skills):
            d = repo if s == name else os.path.join(repo, s)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write("---\nname: %s\ndescription: someone's skill\n---\n" % s)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "remote", "add", "origin", remote % name], cwd=repo, check=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=env)
        for s in skills:
            d = os.path.join(self.claude, "skills", s)
            os.makedirs(d, exist_ok=True)
            os.symlink(os.path.join(repo, s, "SKILL.md"), os.path.join(d, "SKILL.md"))

    def add_to_collection(self, name, skill, author):
        """入れたスキル集に、自分のスキルを足してコミットする（配布元には無い）。"""
        repo = os.path.join(self.claude, "skills", name)
        d = os.path.join(repo, skill)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: %s\ndescription: my own addition\n---\n" % skill)
        env = dict(os.environ, GIT_AUTHOR_NAME="Me", GIT_AUTHOR_EMAIL=author,
                   GIT_COMMITTER_NAME="Me", GIT_COMMITTER_EMAIL=author)
        subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "mine"], cwd=repo, check=True, env=env)
        link = os.path.join(self.claude, "skills", skill)
        os.makedirs(link, exist_ok=True)
        os.symlink(os.path.join(d, "SKILL.md"), os.path.join(link, "SKILL.md"))

    def add_skills_cli(self, name, source="vercel-labs/agent-skills", source_type="github"):
        """`npx skills` で入れたもの: ~/.agents/skills/<name> に置き、~/.claude/skills へフォルダごと symlink。"""
        agents = os.path.join(self.root, ".agents")
        d = os.path.join(agents, "skills", name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: %s\ndescription: from the skills cli\n---\n" % name)
        lock_path = os.path.join(agents, ".skill-lock.json")
        lock = json.load(open(lock_path)) if os.path.exists(lock_path) else {"version": 3, "skills": {}}
        lock["skills"][name] = {"source": source, "sourceType": source_type,
                                "installedAt": "2026-08-01T00:00:00.000Z"}
        with open(lock_path, "w") as f:
            json.dump(lock, f)
        os.symlink(d, os.path.join(self.claude, "skills", name))

    def make_workdir(self, *parts, remote=None):
        d = os.path.join(self.root, *parts)
        os.makedirs(d, exist_ok=True)
        if remote:
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=d, check=True)
            subprocess.run(["git", "remote", "add", "origin", remote], cwd=d, check=True)
        return d

    # ---- 会話の記録 ----
    def user_line(self, day, text, cwd, hh=9, kind="human", entrypoint="cli", **extra):
        self.n += 1
        d = {"parentUuid": None, "isSidechain": False, "type": "user", "uuid": "u%d" % self.n,
             "timestamp": ts(day, hh, self.n % 60), "entrypoint": entrypoint, "cwd": cwd,
             "sessionId": "s1", "version": "2.1.285" if self.form == "new" else "2.0.10",
             "message": {"role": "user", "content": text}}
        if self.form == "new":
            d["origin"] = {"kind": kind}
        d.update(extra)
        return jl(d)

    def tool_use_line(self, day, name, inp, cwd, hh=10):
        self.n += 1
        return jl({"type": "assistant", "uuid": "a%d" % self.n, "timestamp": ts(day, hh, self.n % 60),
                   "cwd": cwd, "isSidechain": False, "entrypoint": "cli",
                   "message": {"role": "assistant", "content": [
                       {"type": "tool_use", "id": "toolu_%d" % self.n, "name": name, "input": inp}]}})

    def tool_result_line(self, day, cwd, text="ファイルの中身: パスワードは hunter2"):
        self.n += 1
        return jl({"type": "user", "uuid": "r%d" % self.n, "timestamp": ts(day, 11, self.n % 60), "cwd": cwd,
                   "isSidechain": False, "entrypoint": "cli", "toolUseResult": {"stdout": text},
                   "message": {"role": "user", "content": [
                       {"type": "tool_result", "tool_use_id": "toolu_x", "content": text}]}})

    def write_session(self, project, lines, name="session1.jsonl"):
        d = os.path.join(self.claude, "projects", project)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return os.path.join(d, name)

    def write_settings(self, **kw):
        with open(os.path.join(self.claude, "settings.json"), "w") as f:
            json.dump(kw, f)


def publish_upstream(repo):
    """今のコミットを配布元の追跡ブランチ（origin/main・origin/HEAD）として記録する（clone した直後と同じ形）。"""
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=repo, check=True)
    subprocess.run(["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"], cwd=repo, check=True)


def make_home(root, size="small", form="new", leaky=False, today=TODAY, seed_transcripts=True):
    n = SIZES[size]
    h = FakeHome(root, form)
    cwd = h.make_workdir("work", "ordinary-app")
    if leaky:
        cwd = h.make_workdir("clients", CUSTOMER + "-corp", "portal-app",
                             remote="git@github.com:tester-org/%s-corp.git" % CUSTOMER)
    names = []
    for i in range(n):
        base = BASE_NAMES[i % len(BASE_NAMES)]
        nm = base if i < len(BASE_NAMES) else "%s-%d" % (base, i)
        if leaky and i == 0:
            nm = "%s-invoice" % CUSTOMER
        names.append(nm)
        desc = "%s の定型作業" % nm
        if leaky and i == 0:
            desc = "Globex 商事向けの請求書を作る"
        h.add_skill(nm, desc)
    if n:
        h.git_commit_claude()
    if size != "tiny":
        h.add_plugin("code-review", "claude-plugins-official", ["review"], repo="anthropics/claude-plugins-official")
        h.add_plugin("mine-tools", "local-market", ["secret-tool"])
    h.finish_plugins()
    h.write_settings(cleanupPeriodDays=365)

    if seed_transcripts and size != "tiny":
        lines = []
        start = today - timedelta(days=60)
        per_day = {"small": 2, "medium": 6, "large": 20}[size]
        for dd in range(61):
            day = start + timedelta(days=dd)
            for k in range(per_day):
                text = TYPED_SAMPLES[(dd + k) % len(TYPED_SAMPLES)] + " #%d" % (dd * 100 + k)  # 同じ本文は1件にまとめるので番号で分ける
                lines.append(h.user_line(day, text, cwd))
                nm = names[(dd * 7 + k) % len(names)]
                lines.append(h.tool_use_line(day, "Skill", {"skill": nm}, cwd))
        lines.append(h.tool_use_line(today, "Skill", {"skill": "code-review:review"}, cwd))
        lines.append(h.tool_use_line(today, "Agent", {"subagent_type": "general-purpose", "prompt": "x"}, cwd))
        lines.append(h.tool_use_line(today, "Agent", {"subagent_type": "%s-helper" % (CUSTOMER if leaky else "my"), "prompt": "x"}, cwd))
        h.write_session("-work-project", lines)
    return h


if __name__ == "__main__":
    out = sys.argv[1]
    size = sys.argv[2] if len(sys.argv) > 2 else "small"
    form = sys.argv[3] if len(sys.argv) > 3 else "new"
    make_home(out, size, form, leaky="--leaky" in sys.argv)
    print(os.path.join(out, ".claude"))
