import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import collect  # noqa: E402
import fakes  # noqa: E402

TODAY = fakes.TODAY


def run_collect(home, history=None):
    out = os.path.join(home.root, "work")
    c = collect.collect(home.claude, out, TODAY, history_path=history, log=lambda *a: None)
    with open(os.path.join(out, "typed.txt"), encoding="utf-8") as f:
        typed = [ln.rstrip("\n").split("\t", 2) for ln in f if ln.strip()]
    return c, typed, out


def skill(c, sid):
    return next(s for s in c["skills"] if s["id"] == sid)


class TypedExtraction(unittest.TestCase):
    """人が打った文の抜き出し（R2-3）。人の文だけが残る。"""

    def build(self, form):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.FakeHome(tmp.name, form)
        cwd = h.make_workdir("w", "app")
        d = TODAY - timedelta(days=3)
        lines = [
            h.user_line(d, "人が打った文その1", cwd),
            h.user_line(d, "自動実行の文", cwd, entrypoint="sdk-cli"),
            h.tool_result_line(d, cwd),
            h.user_line(d, "<command-name>/clear</command-name>\n<command-message>clear</command-message>", cwd),
            h.user_line(d, "<task-notification>裏の処理の知らせ</task-notification>", cwd, kind="task-notification"),
            h.user_line(d, "スキル本文の展開", cwd, isMeta=True),
            h.user_line(d, "<system-reminder>差し込まれた文</system-reminder>", cwd),
            h.user_line(d, "サブエージェントの文", cwd, isSidechain=True),
            h.user_line(d, "人が打った文その2 sk-abcdefghijklmnop1234567890 を使う", cwd),
        ]
        h.write_session("-w-app", lines)
        return h

    def check(self, form):
        h = self.build(form)
        c, typed, _ = run_collect(h)
        texts = [t[2] for t in typed]
        self.assertEqual(len(texts), 2, texts)
        self.assertEqual(texts[0], "人が打った文その1")
        self.assertIn("人が打った文その2", texts[1])
        self.assertNotIn("sk-abcdefghijklmnop1234567890", texts[1])  # 鍵らしい文字は伏せる
        self.assertNotIn("hunter2", "\n".join(texts))
        self.assertEqual(c["typed"]["total_30d"], 2)

    def test_new_form(self):
        self.check("new")

    def test_old_form_without_origin(self):
        self.check("old")

    def test_zero_found_is_flagged(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.FakeHome(tmp.name)
        cwd = h.make_workdir("w")
        h.write_session("-w", [h.tool_result_line(TODAY, cwd)])
        c, _, _ = run_collect(h)
        self.assertTrue(c["typed"]["has_transcripts"])
        self.assertFalse(c["typed"]["found"])
        self.assertIn("見つけられませんでした", collect.summary(c))

    def test_outside_window_not_used(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.FakeHome(tmp.name)
        cwd = h.make_workdir("w")
        h.write_session("-w", [h.user_line(TODAY - timedelta(days=45), "四十五日前の古い文です", cwd),
                               h.user_line(TODAY - timedelta(days=29), "ぎりぎり期間に入る文です", cwd),
                               h.user_line(TODAY - timedelta(days=30), "ぎりぎり期間に入らない文", cwd)])
        c, typed, _ = run_collect(h)
        self.assertEqual([t[2] for t in typed], ["ぎりぎり期間に入る文です"])


class BrokenLines(unittest.TestCase):
    def test_broken_lines_skipped_and_counted(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.FakeHome(tmp.name)
        cwd = h.make_workdir("w")
        good = h.user_line(TODAY, "これは正常に読める文です", cwd)
        h.write_session("-w", [good, good[: len(good) // 2], '{"type":"user","timestamp":"2026-10-01T00:00:00.000Z","message":{', good])
        c, typed, _ = run_collect(h)
        self.assertEqual(c["stats"]["bad_lines"], 2)
        self.assertEqual(len(typed), 1)  # 同じ uuid は1回だけ


class TypedNoise(unittest.TestCase):
    def test_compact_summary_is_not_typed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.FakeHome(tmp.name, "old")  # 要約の行には origin が無い
        h.finish_plugins()
        cwd = h.make_workdir("w")
        summ = "This session is being continued from a previous conversation that ran out of context."
        h.write_session("-w", [h.user_line(TODAY, summ, cwd, isCompactSummary=True, isVisibleInTranscriptOnly=True),
                               h.user_line(TODAY, summ + " 印の無い古い形", cwd),
                               h.user_line(TODAY, "今月の請求書を作ってください", cwd)])
        c, typed, _ = run_collect(h)
        self.assertEqual([t[2] for t in typed], ["今月の請求書を作ってください"])

    def test_same_text_is_sampled_once(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.FakeHome(tmp.name)
        h.finish_plugins()
        cwd = h.make_workdir("w")
        h.write_session("-w", [h.user_line(TODAY, "説明会の資料をまとめて", cwd, hh=9),
                               h.user_line(TODAY, "説明会の資料をまとめて", cwd, hh=9),  # 再送
                               h.user_line(TODAY - timedelta(days=1), "別の作業を頼みたい", cwd)])
        c, typed, _ = run_collect(h)
        self.assertEqual(sorted(t[2] for t in typed), ["別の作業を頼みたい", "説明会の資料をまとめて"])
        self.assertEqual(c["typed"]["total_30d"], 3)  # 件数は打った回数のまま（求人票の見積もりに使う）


class Sampling(unittest.TestCase):
    def test_300_or_fewer_are_all_used(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.make_home(tmp.name, "small")
        c, typed, _ = run_collect(h)
        self.assertEqual(c["typed"]["sample_n"], c["typed"]["total_30d"])
        self.assertEqual(len(typed), c["typed"]["total_30d"])

    def test_over_300_spread_evenly_over_days(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.make_home(tmp.name, "large")  # 1日20件 x 30日 = 600件
        c, typed, _ = run_collect(h)
        self.assertEqual(c["typed"]["total_30d"], 600)
        self.assertEqual(len(typed), 300)
        per_day = {}
        for _, d, _ in typed:
            per_day[d] = per_day.get(d, 0) + 1
        self.assertEqual(len(per_day), 30)
        self.assertEqual(set(per_day.values()), {10})

    def test_short_days_leave_room_for_busy_days(self):
        items = [(date(2026, 10, 1), "t%03d" % i, "x") for i in range(500)] + \
                [(date(2026, 10, 2), "u%03d" % i, "y") for i in range(5)]
        sel = collect.select_even(items, 300)
        self.assertEqual(len(sel), 300)
        self.assertEqual(sum(1 for s in sel if s[0] == date(2026, 10, 2)), 5)

    def test_char_cap_reduces_count(self):
        items = [(date(2026, 10, 1) + timedelta(days=i % 20), "t%05d" % i, "あ" * 200) for i in range(300)]
        sel = collect.choose_typed(items, 300, 30000)
        self.assertLessEqual(sum(len(s[2]) for s in sel), 30000)
        self.assertGreater(len(sel), 100)

    def test_item_cut_at_200_chars(self):
        self.assertEqual(len(collect.clean_typed("あ" * 500)), 200)


class JoinDates(unittest.TestCase):
    def test_own_skill_uses_first_commit_plugin_uses_installed_at(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.make_home(tmp.name, "small", seed_transcripts=False)
        c, _, _ = run_collect(h)
        self.assertEqual(skill(c, "invoice-maker")["joined"], "2026-06-19")  # git の初回コミット
        self.assertEqual(skill(c, "code-review:review")["joined"], "2026-09-01")  # installedAt
        self.assertTrue(skill(c, "code-review:review")["public"])
        self.assertFalse(skill(c, "mine-tools:secret-tool")["public"])  # 公式でない配布元は自作扱い

    def test_file_date_when_not_in_git(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.FakeHome(tmp.name)
        d = h.add_skill("alone")
        old = datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()  # TODAY より前に置いたことにする
        os.utime(os.path.join(d, "SKILL.md"), (old, old))
        h.finish_plugins()
        c, _, _ = run_collect(h)
        self.assertIsNotNone(skill(c, "alone")["joined"])
        self.assertLessEqual(date.fromisoformat(skill(c, "alone")["joined"]), TODAY)

    def test_future_dates_dropped(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.FakeHome(tmp.name)
        h.add_plugin("p", "m", ["s"], installed_at="2099-01-01T00:00:00.000Z")
        h.finish_plugins()
        cwd = h.make_workdir("w")
        h.write_session("-w", [h.tool_use_line(date(2026, 9, 10), "Skill", {"skill": "p:s"}, cwd)])
        c, _, _ = run_collect(h)
        self.assertEqual(skill(c, "p:s")["joined"], "2026-09-10")  # 材料が使えないので最初に使われた日

    def test_joined_is_earlier_of_material_and_first_use(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.FakeHome(tmp.name)
        h.add_plugin("p", "m", ["s"], installed_at="2026-09-20T00:00:00.000Z")
        h.finish_plugins()
        cwd = h.make_workdir("w")
        h.write_session("-w", [h.tool_use_line(date(2026, 9, 10), "Skill", {"skill": "p:s"}, cwd)])
        c, _, _ = run_collect(h)
        self.assertEqual(skill(c, "p:s")["joined"], "2026-09-10")

    def test_history_join_date_is_never_overwritten_by_later_date(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.FakeHome(tmp.name)
        h.add_plugin("p", "m", ["s"], installed_at="2026-09-20T00:00:00.000Z")
        h.finish_plugins()
        hist = os.path.join(tmp.name, "history.jsonl")
        with open(hist, "w") as f:
            f.write(json.dumps({"date": "2026-09-01", "employees": {"p:s": ["2026-08-15", "2026-08-30", 3]}}) + "\n")
        c, _, _ = run_collect(h, history=hist)
        self.assertEqual(skill(c, "p:s")["joined"], "2026-08-15")
        self.assertEqual(skill(c, "p:s")["last_used"], "2026-08-30")
        self.assertEqual(c["founded"], "2026-08-15")


class WhoMadeIt(unittest.TestCase):
    """自作・入れたスキル集・このプラグイン自身の見分け（2026-10-09 の実地インストールで見つかった直し）。"""

    def home(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.FakeHome(tmp.name)
        h.add_skill("my-invoice")
        h.git_commit_claude()
        return h

    def test_collection_from_someone_elses_repo_is_not_own(self):
        h = self.home()
        h.add_collection("gstack", ["ship", "qa"], remote="git@github.com:garrytan/%s.git")
        fakes.publish_upstream(os.path.join(h.claude, "skills", "gstack"))
        h.finish_plugins()
        c, _, _ = run_collect(h)
        self.assertEqual(skill(c, "my-invoice")["kind"], "own")
        self.assertFalse(skill(c, "my-invoice")["public"])
        for sid in ("gstack", "ship", "qa"):
            self.assertEqual(skill(c, sid)["kind"], "external", sid)
            self.assertTrue(skill(c, sid)["public"], sid)
        self.assertIn("自作 1・入れたスキル集 3", collect.summary(c))

    def test_own_addition_to_public_collection_is_hidden(self):
        """公開のスキル集に別のアドレスで自分のスキルを足しても、配布元に無いので実名は出さない（セキュリティ担当の指摘）。"""
        h = self.home()
        h.add_collection("gstack", ["ship"], remote="git@github.com:garrytan/%s.git")
        repo = os.path.join(h.claude, "skills", "gstack")
        fakes.publish_upstream(repo)
        h.add_to_collection("gstack", "my-secret-flow", author="old-address@example.invalid")
        h.finish_plugins()
        c, _, _ = run_collect(h)
        self.assertTrue(skill(c, "ship")["public"])
        s = skill(c, "my-secret-flow")
        self.assertEqual((s["kind"], s["public"]), ("external", False))

    def test_typed_file_is_readable_only_by_owner(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.make_home(tmp.name, "small")
        _, _, out = run_collect(h)
        self.assertEqual(os.stat(os.path.join(out, "typed.txt")).st_mode & 0o777, 0o600)

    def test_my_own_repo_linked_in_stays_own(self):
        h = self.home()
        h.add_collection("dotfiles", ["mine"], author="t@example.invalid")  # 自分の作を symlink で入れた
        h.finish_plugins()
        c, _, _ = run_collect(h)
        self.assertEqual(skill(c, "mine")["kind"], "own")
        self.assertFalse(skill(c, "mine")["public"])

    def test_collection_not_known_public_is_hidden(self):
        """GitHub にあっても非公開のリポジトリかもしれない。公開の一覧に無い配布元は伏せる。"""
        h = self.home()
        h.add_collection("teamkit", ["deploy"], remote="git@github.com:acme/private-%s.git")
        h.add_collection("hostkit", ["sync"], remote="git@github.com.corp.example.invalid:garrytan/%s.git")
        h.add_skills_cli("team-tips", source="acme/private-skills")
        h.finish_plugins()
        c, _, _ = run_collect(h)
        for sid in ("deploy", "sync", "team-tips"):
            self.assertEqual(skill(c, sid)["kind"], "external", sid)
            self.assertFalse(skill(c, sid)["public"], sid)

    def test_skills_cli_install_is_not_own(self):
        h = self.home()
        h.add_skills_cli("react-tips")
        h.finish_plugins()
        c, _, _ = run_collect(h)
        s = skill(c, "react-tips")
        self.assertEqual((s["kind"], s["public"], s["joined"]), ("external", True, "2026-08-01"))

    def test_own_skill_named_like_this_plugin_is_counted(self):
        h = self.home()
        h.add_skill("ai-company")
        h.finish_plugins()
        cwd = h.make_workdir("w")
        h.write_session("-w", [h.tool_use_line(TODAY, "Skill", {"skill": "ai-company"}, cwd)])
        c, _, _ = run_collect(h)
        self.assertEqual(skill(c, "ai-company")["count_total"], 1)

    def test_lock_beside_project_does_not_turn_own_skill_external(self):
        """skills-lock.json は .agents/skills に置かれたものにだけ効く。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.FakeHome(tmp.name)
        h.finish_plugins()
        proj = h.make_workdir("p")
        d = os.path.join(proj, ".claude", "skills", "mine")
        os.makedirs(d)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: mine\n---\n")
        with open(os.path.join(proj, "skills-lock.json"), "w") as f:
            json.dump({"version": 1, "skills": {"mine": {"source": "x/y", "sourceType": "github"}}}, f)
        h.write_session("-p", [h.user_line(TODAY, "このフォルダで作業します", proj)])
        c, _, _ = run_collect(h)
        self.assertEqual(skill(c, "mine")["kind"], "own")

    def test_this_plugin_is_not_an_employee(self):
        h = self.home()
        h.add_plugin("ai-company", "ai-company", ["ai-company"], repo="khara2506/ai-company")
        h.finish_plugins()
        cwd = h.make_workdir("w")
        h.write_session("-w", [h.tool_use_line(TODAY, "Skill", {"skill": "ai-company:ai-company"}, cwd),
                               h.user_line(TODAY, "<command-name>/ai-company</command-name>", cwd)])
        c, _, _ = run_collect(h)
        self.assertNotIn("ai-company:ai-company", [s["id"] for s in c["skills"]])
        self.assertEqual(c["stats"]["unmatched_calls"], 0)


class ProjectSkills(unittest.TestCase):
    def test_project_skills_of_worked_folders_are_counted(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.FakeHome(tmp.name)
        h.add_skill("alpha")
        h.finish_plugins()
        proj = h.make_workdir("hq")
        sub = h.make_workdir("hq", "sub")
        d = os.path.join(proj, ".claude", "skills", "tax-helper")
        os.makedirs(d)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: tax-helper\ndescription: 税の確認\n---\n")
        h.write_session("-hq-sub", [h.tool_use_line(TODAY, "Skill", {"skill": "tax-helper"}, sub)])
        c, _, _ = run_collect(h)
        s = skill(c, "tax-helper")
        self.assertEqual((s["kind"], s["public"], s["count_total"]), ("own", False, 1))
        self.assertEqual(sum(1 for x in c["skills"] if x["id"] == "alpha"), 1)  # ~/.claude/skills は二重に数えない

    def test_skills_cli_install_in_project_is_not_own(self):
        """`npx skills` をプロジェクトで使うと <proj>/.agents/skills に置かれ、記録は <proj>/skills-lock.json。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.FakeHome(tmp.name)
        h.finish_plugins()
        proj = h.make_workdir("studio")
        real = os.path.join(proj, ".agents", "skills", "video-tips")
        os.makedirs(real)
        with open(os.path.join(real, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: video-tips\n---\n")
        with open(os.path.join(proj, "skills-lock.json"), "w") as f:
            json.dump({"version": 1, "skills": {"video-tips": {"source": "remotion-dev/skills", "sourceType": "github"}}}, f)
        os.makedirs(os.path.join(proj, ".claude", "skills"))
        os.symlink(real, os.path.join(proj, ".claude", "skills", "video-tips"))
        h.write_session("-studio", [h.user_line(TODAY, "動画を作りたいので手伝って", proj)])
        c, _, _ = run_collect(h)
        self.assertEqual((skill(c, "video-tips")["kind"], skill(c, "video-tips")["public"]), ("external", True))

    def test_worktree_copies_are_not_read(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.FakeHome(tmp.name)
        h.finish_plugins()
        wt = h.make_workdir("app", ".claude", "worktrees", "fix-1")
        d = os.path.join(wt, ".claude", "skills", "only-in-worktree")
        os.makedirs(d)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: only-in-worktree\n---\n")
        h.write_session("-app", [h.user_line(TODAY, "作業フォルダで直して", wt)])
        c, _, _ = run_collect(h)
        self.assertEqual(c["skills"], [])  # 作業用の写し（git worktree）は本体と同じなので数えない

    def test_another_home_above_the_work_folder_is_not_a_project(self):
        """作業フォルダの上に別のホームの ~/.claude（projects/ がある）があっても、そのスキルは数えない。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        outer = os.path.join(tmp.name, ".claude")
        os.makedirs(os.path.join(outer, "projects"))
        d = os.path.join(outer, "skills", "outer-skill")
        os.makedirs(d)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: outer-skill\n---\n")
        h = fakes.FakeHome(os.path.join(tmp.name, "inner"))
        h.finish_plugins()
        cwd = h.make_workdir("w")
        h.write_session("-w", [h.user_line(TODAY, "このフォルダで作業します", cwd)])
        c, _, _ = run_collect(h)
        self.assertEqual(c["skills"], [])

    def test_folder_never_worked_in_is_not_read(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.FakeHome(tmp.name)
        h.finish_plugins()
        d = os.path.join(h.make_workdir("other"), ".claude", "skills", "x-skill")
        os.makedirs(d)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: x-skill\n---\n")
        c, _, _ = run_collect(h)
        self.assertEqual(c["skills"], [])


class Counting(unittest.TestCase):
    def test_counts_skill_tool_slash_and_agents(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.FakeHome(tmp.name)
        h.add_skill("alpha")
        h.add_plugin("pl", "official", ["beta"], repo="x/y")
        h.finish_plugins()
        cwd = h.make_workdir("w")
        d = TODAY - timedelta(days=2)
        old = TODAY - timedelta(days=50)
        h.write_session("-w", [
            h.tool_use_line(d, "Skill", {"skill": "alpha"}, cwd),
            h.tool_use_line(old, "Skill", {"skill": "alpha"}, cwd),
            h.user_line(d, "<command-name>/alpha</command-name>\n<command-args></command-args>", cwd),
            h.user_line(d, "<command-name>/pl:beta</command-name>", cwd),
            h.user_line(d, "<command-name>/clear</command-name>", cwd),
            h.tool_use_line(d, "Agent", {"subagent_type": "Explore"}, cwd),
            h.tool_use_line(d, "Agent", {"subagent_type": "Explore"}, cwd),
            h.tool_use_line(d, "Agent", {"subagent_type": "customer-x-bot"}, cwd),
        ])
        c, _, _ = run_collect(h)
        a = skill(c, "alpha")
        self.assertEqual((a["count_30d"], a["count_total"]), (2, 3))
        self.assertEqual(a["first_used"], old.isoformat())
        self.assertEqual(skill(c, "pl:beta")["count_30d"], 1)
        ag = {x["id"]: x for x in c["agents"]}
        self.assertEqual(ag["Explore"]["count_30d"], 2)
        self.assertTrue(ag["Explore"]["builtin"])
        self.assertTrue(ag["customer-x-bot"]["own"])
        self.assertEqual(c["stats"]["unmatched_calls"], 1)  # /clear

    def test_builtin_skills_are_outside_staff(self):
        """Claude Code に最初から入っているスキルは、組み込みのサブエージェントと同じく外部スタッフ（実名・社員数に入れない）。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.FakeHome(tmp.name)
        h.add_skill("simplify")  # 同じ名前の自作スキルがあればそちらに数える
        h.finish_plugins()
        cwd = h.make_workdir("w")
        h.write_session("-w", [
            h.user_line(TODAY, "<command-name>/code-review</command-name>", cwd),
            h.tool_use_line(TODAY, "Skill", {"skill": "code-review"}, cwd),
            h.tool_use_line(TODAY, "Skill", {"skill": "anthropic-skills:pptx"}, cwd),
            h.tool_use_line(TODAY, "Skill", {"skill": "simplify"}, cwd),
            h.user_line(TODAY, "<command-name>/clear</command-name>", cwd),
        ])
        c, _, _ = run_collect(h)
        ag = {x["id"]: x for x in c["agents"]}
        self.assertEqual(ag["/code-review"]["count_30d"], 2)
        self.assertTrue(ag["/code-review"]["builtin"] and ag["/code-review"]["public"])
        self.assertFalse(ag["/code-review"]["own"])
        self.assertIn("/anthropic-skills:pptx", ag)
        self.assertNotIn("/simplify", ag)
        self.assertEqual(skill(c, "simplify")["count_total"], 1)
        self.assertEqual([s["id"] for s in c["skills"]], ["simplify"])
        self.assertEqual(c["stats"]["unmatched_calls"], 1)  # /clear

    def test_subagent_transcripts_are_not_read(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.FakeHome(tmp.name)
        h.add_skill("alpha")
        h.finish_plugins()
        cwd = h.make_workdir("w")
        h.write_session("-w", [h.user_line(TODAY, "本線の文", cwd)])
        sub = os.path.join(h.claude, "projects", "-w", "s1", "subagents")
        os.makedirs(sub)
        with open(os.path.join(sub, "agent-1.jsonl"), "w") as f:
            f.write(h.tool_use_line(TODAY, "Skill", {"skill": "alpha"}, cwd) + "\n")
        c, _, _ = run_collect(h)
        self.assertEqual(skill(c, "alpha")["count_total"], 0)

    def test_automated_runs_count_for_skills_but_not_for_typed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.FakeHome(tmp.name)
        h.add_skill("alpha")
        h.finish_plugins()
        cwd = h.make_workdir("w")
        auto = h.tool_use_line(TODAY, "Skill", {"skill": "alpha"}, cwd).replace('"entrypoint":"cli"', '"entrypoint":"sdk-cli"')
        h.write_session("-w", [auto, h.user_line(TODAY, "自動の文", cwd, entrypoint="sdk-cli")])
        c, typed, _ = run_collect(h)
        self.assertEqual(skill(c, "alpha")["count_total"], 1)
        self.assertEqual(typed, [])


    def test_short_replies_are_not_typed(self):
        """「はい」「続けて」などの短い返事は作業の種類が分からないので抜き出さない（2026-10-09 本人の環境で半分強がこれだった）。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.FakeHome(tmp.name)
        h.finish_plugins()
        cwd = h.make_workdir("w")
        h.write_session("-w", [h.user_line(TODAY, "はい", cwd), h.user_line(TODAY, "続けて", cwd, hh=10),
                               h.user_line(TODAY, "今月の請求書を作ってください", cwd, hh=11)])
        c, typed, _ = run_collect(h)
        self.assertEqual([t[2] for t in typed], ["今月の請求書を作ってください"])
        self.assertEqual(c["typed"]["total_30d"], 1)

class Retirement(unittest.TestCase):
    def test_missing_skill_becomes_retired_with_date(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.FakeHome(tmp.name)
        h.add_skill("stay")
        h.finish_plugins()
        hist = os.path.join(tmp.name, "history.jsonl")
        rows = [{"date": "2026-09-01", "employees": {"stay": ["2026-08-01", None, 0], "gone": ["2026-08-01", "2026-08-20", 2]}},
                {"date": "2026-09-15", "employees": {"stay": ["2026-08-01", None, 0], "gone": ["2026-08-01", "2026-08-20", 2]}}]
        with open(hist, "w") as f:
            f.write("\n".join(json.dumps(r) for r in rows) + "\n")
        c, _, _ = run_collect(h, history=hist)
        self.assertEqual([r["id"] for r in c["retired"]], ["gone"])
        self.assertEqual(c["retired"][0]["retired_on"], TODAY.isoformat())
        self.assertEqual(c["retired"][0]["joined"], "2026-08-01")


class Empty(unittest.TestCase):
    def test_no_skills_no_transcripts_does_not_stop(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.make_home(tmp.name, "tiny")
        c, typed, out = run_collect(h)
        self.assertEqual(c["skills"], [])
        self.assertEqual(c["stats"]["files"], 0)
        self.assertEqual(typed, [])
        self.assertTrue(os.path.exists(os.path.join(out, "company.json")))
        self.assertEqual(c["founded"], TODAY.isoformat())

    def test_missing_claude_dir_does_not_stop(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        c = collect.collect(os.path.join(tmp.name, "nothing"), os.path.join(tmp.name, "o"), TODAY, log=lambda *a: None)
        self.assertEqual(c["skills"], [])


class PrivacyOfWorkFile(unittest.TestCase):
    def test_company_json_has_no_typed_text(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.make_home(tmp.name, "small")
        c, typed, out = run_collect(h)
        raw = open(os.path.join(out, "company.json"), encoding="utf-8").read()
        for sample in fakes.TYPED_SAMPLES:
            self.assertNotIn(sample, raw)
        self.assertTrue(typed)

    def test_seeds_contain_customer_path_and_remote(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        h = fakes.make_home(tmp.name, "small", leaky=True)
        c, _, _ = run_collect(h)
        joined = " ".join(c["seeds"]["paths"]).lower()
        self.assertIn(fakes.CUSTOMER, joined)
        self.assertIn("tester-org", " ".join(c["seeds"]["remotes"]))


class PluginFiles(unittest.TestCase):
    def test_plugin_files_are_valid(self):
        root = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
        mk = json.load(open(os.path.join(root, ".claude-plugin", "marketplace.json"), encoding="utf-8"))
        pj = json.load(open(os.path.join(root, "plugins", "ai-company", ".claude-plugin", "plugin.json"), encoding="utf-8"))
        self.assertEqual(mk["plugins"][0]["name"], pj["name"])
        self.assertEqual(mk["plugins"][0]["version"], pj["version"])
        self.assertTrue(os.path.isfile(os.path.join(root, "plugins", "ai-company", "skills", "ai-company", "SKILL.md")))


if __name__ == "__main__":
    unittest.main()
