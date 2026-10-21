import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import collect  # noqa: E402
import exclude  # noqa: E402
import fakes  # noqa: E402
import render  # noqa: E402

TODAY = fakes.TODAY
VOCAB = render.load_vocab()
DAY = TODAY - timedelta(days=2)


class Base(unittest.TestCase):
    """~/work/app（ふつうの仕事）と ~/clients/globex-corp/portal（顧客の仕事）で打った記録を持つ利用者。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.h = fakes.FakeHome(os.path.join(self.root, "home"))
        self.clients = os.path.join(self.h.root, "clients")
        self.app = self.h.make_workdir("work", "app")
        self.client_app = self.h.make_workdir("clients", "globex-corp", "portal")
        self.out = os.path.join(self.root, "ai-company")
        self.work = os.path.join(self.out, "work")

    def session(self, cwd, lines, name="s.jsonl"):
        return self.h.write_session(exclude.folder_key(cwd), lines, name)

    def collect(self, dirs=()):
        return collect.collect(self.h.claude, self.work, TODAY, history_path=os.path.join(self.out, "history.jsonl"),
                               log=lambda *a: None, skip=exclude.Excluder(dirs))

    def typed(self):
        with open(os.path.join(self.work, "typed.txt"), encoding="utf-8") as f:
            return f.read()

    def main(self, *args):
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = collect.main(["--claude-dir", self.h.claude, "--out-dir", self.work,
                               "--today", TODAY.isoformat()] + list(args))
        return rc, buf.getvalue() + err.getvalue()


class SkipFolders(Base):
    def test_client_folder_is_not_read_and_others_are(self):
        self.session(self.app, [self.h.user_line(DAY, "いつもの週報をまとめて", self.app)])
        self.session(self.client_app, [self.h.user_line(DAY, "Globex 様への見積もりを書いて", self.client_app)])
        c = self.collect([self.clients])
        self.assertIn("週報", self.typed())
        self.assertNotIn("Globex", self.typed())
        self.assertEqual({k: v for k, v in c["exclude"].items() if k != "drop"}, {"dirs": 1, "folders": 1, "files": 1, "lines": 0})
        self.assertFalse(any("globex" in t.lower() for t in c["seeds"]["paths"]))

    def test_lines_after_cd_into_client_folder_are_not_counted(self):
        # ~/work/app で始めた会話の途中で顧客のフォルダに入った。cwd の無い行も、その前の cwd に従う
        no_cwd = json.loads(self.h.user_line(DAY, "この顧客の請求書を作って", self.client_app))
        del no_cwd["cwd"]
        self.session(self.app, [
            self.h.user_line(DAY, "いつもの週報をまとめて", self.app),
            self.h.user_line(DAY, "Globex 様への見積もりを書いて", self.client_app),
            json.dumps(no_cwd, ensure_ascii=False),
            self.h.tool_use_line(DAY, "Skill", {"skill": "client-only"}, self.client_app),
        ])
        c = self.collect([self.clients])
        self.assertIn("週報", self.typed())
        self.assertNotIn("Globex", self.typed())
        self.assertNotIn("請求書", self.typed())
        self.assertNotIn(self.client_app, collect.read_transcripts(self.h.claude, TODAY, lambda *a: None,
                                                                 exclude.Excluder([self.clients]))["cwds"])
        self.assertEqual(c["exclude"]["lines"], 3)
        self.assertEqual(c["exclude"]["folders"], 0)

    def test_symlinked_start_folder_is_still_excluded(self):
        link = os.path.join(self.h.root, "shortcut")
        os.symlink(self.client_app, link)  # 顧客のフォルダへのリンクから起動した
        self.session(link, [self.h.user_line(DAY, "Globex 様への見積もりを書いて", link)])
        c = self.collect([self.clients])
        self.assertNotIn("Globex", self.typed())
        self.assertEqual(c["exclude"]["lines"], 1)

    def test_exclude_given_through_a_link_matches_the_real_path(self):
        link = os.path.join(self.h.root, "customers")
        os.symlink(self.clients, link)
        self.session(self.client_app, [self.h.user_line(DAY, "Globex 様への見積もりを書いて", self.client_app)])
        self.collect([link])
        self.assertNotIn("Globex", self.typed())

    def test_case_is_ignored_where_the_disk_ignores_it(self):
        self.session(self.client_app, [self.h.user_line(DAY, "Globex 様への見積もりを書いて", self.client_app)])
        with mock.patch.object(exclude, "CASE_INSENSITIVE_FS", True):
            self.collect([self.clients.replace("clients", "Clients")])
        self.assertNotIn("Globex", self.typed())

    def test_root_excludes_everything(self):
        self.session(self.app, [self.h.user_line(DAY, "いつもの週報をまとめて", self.app)])
        c = self.collect(["/"])
        self.assertEqual(self.typed(), "")
        self.assertIn("すべての記録を外しました", collect.summary(c))

    def test_project_skills_and_old_employees_under_client_folder_are_dropped(self):
        d = os.path.join(self.client_app, ".claude", "skills", "globex-invoice")
        os.makedirs(d)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: globex-invoice\ndescription: Globex 向けの請求書\n---\n")
        self.session(self.client_app, [self.h.user_line(DAY, "Globex 様への見積もりを書いて", self.client_app)])
        before = self.collect()
        self.assertIn("globex-invoice", [s["id"] for s in before["skills"]])
        os.makedirs(self.out, exist_ok=True)
        with open(os.path.join(self.out, "history.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"date": (TODAY - timedelta(days=1)).isoformat(),
                                "employees": {"globex-invoice": ["2026-09-01", "2026-10-01", 3]}}) + "\n")
        after = self.collect([self.clients])
        self.assertNotIn("globex-invoice", [s["id"] for s in after["skills"]])
        self.assertNotIn("globex-invoice", [r["id"] for r in after["retired"]])

    def test_deep_client_skill_seen_before_is_not_retired(self):
        deep = self.h.make_workdir("clients", "globex-corp", "a", "b", "c", "repo")  # 除外から深さ5
        d = os.path.join(deep, ".claude", "skills", "globex-deep")
        os.makedirs(d)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: globex-deep\ndescription: x\n---\n")
        self.session(deep, [self.h.user_line(DAY, "Globex 様への見積もりを書いて", deep)])
        os.makedirs(self.out, exist_ok=True)
        with open(os.path.join(self.out, "history.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"date": (TODAY - timedelta(days=1)).isoformat(),
                                "employees": {"globex-deep": ["2026-09-01", "2026-10-01", 3]}}) + "\n")
        after = self.collect([self.clients])
        self.assertNotIn("globex-deep", [r["id"] for r in after["retired"]])
        self.assertIn(exclude.name_hash("globex-deep"), after["exclude"]["drop"])

    def test_unreadable_folder_under_exclude_is_skipped_quietly(self):
        locked = os.path.join(self.client_app, ".claude", "skills")
        os.makedirs(locked)
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o700)
        if os.access(locked, os.R_OK):
            self.skipTest("root では権限で読めなくできない")
        self.session(self.client_app, [self.h.user_line(DAY, "Globex 様への見積もりを書いて", self.client_app)])
        rc, out = self.main("--exclude", self.clients)
        self.assertEqual(rc, 0)
        self.assertNotIn("globex", out.lower())


class Settings(Base):
    def write(self, text):
        os.makedirs(self.out, exist_ok=True)
        with open(os.path.join(self.out, exclude.FILE_NAME), "w", encoding="utf-8") as f:
            f.write(text)

    def test_exclude_file_is_used(self):
        self.write(json.dumps({"version": 1, "dirs": [self.clients]}))
        self.session(self.client_app, [self.h.user_line(DAY, "Globex 様への見積もりを書いて", self.client_app)])
        rc, out = self.main()
        self.assertEqual(rc, 0)
        self.assertNotIn("Globex", self.typed())
        self.assertIn("読まない作業フォルダ 1 か所", out)

    def test_broken_exclude_file_stops_before_reading_and_leaves_no_typed(self):
        os.makedirs(self.work, exist_ok=True)
        with open(os.path.join(self.work, "typed.txt"), "w", encoding="utf-8") as f:
            f.write("1\t2026-10-01\t前の実行の抜き出し\n")
        self.session(self.client_app, [self.h.user_line(DAY, "Globex 様への見積もりを書いて", self.client_app)])
        for bad in ('{"dirs": ["%s",]}' % self.clients, '{"dirs": "%s"}' % self.clients, '[]', '{"dirs": [""]}'):
            self.write(bad)
            rc, out = self.main()
            self.assertEqual(rc, 2, bad)
            self.assertIn("STATUS: error", out)
            self.assertFalse(os.path.exists(os.path.join(self.work, "typed.txt")), bad)
            self.assertFalse(os.path.exists(os.path.join(self.work, "company.json")), bad)

    def test_typo_is_reported(self):
        self.session(self.client_app, [self.h.user_line(DAY, "Globex 様への見積もりを書いて", self.client_app)])
        rc, out = self.main("--exclude", os.path.join(self.h.root, "client"))
        self.assertEqual(rc, 0)
        self.assertIn("見つかりません", out)
        self.assertIn("どの記録にも当たっていません", out)


class Render(Base):
    def prepare(self, org):
        self.session(self.app, [self.h.user_line(DAY, "いつもの週報をまとめて", self.app)])
        os.makedirs(self.out, exist_ok=True)
        with open(os.path.join(self.out, exclude.FILE_NAME), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "dirs": [self.clients]}, f)
        self.assertEqual(self.main()[0], 0)
        with open(os.path.join(self.out, "org.json"), "w", encoding="utf-8") as f:
            f.write(org if isinstance(org, str) else json.dumps(org, ensure_ascii=False))

    def render(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = render.main(["--work-dir", self.work, "--out-dir", self.out] + list(args))
        return rc, buf.getvalue()

    def test_broken_org_stops_and_is_not_overwritten(self):
        self.prepare('{"company_name": "わたしの会社",}')
        rc, out = self.render()
        self.assertEqual(rc, 2)
        self.assertIn("STATUS: error", out)
        with open(os.path.join(self.out, "org.json"), encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"company_name": "わたしの会社",}')

    def test_client_folder_name_in_share_version_is_caught(self):
        self.prepare({"company_name": "globex の AI 会社", "people": {}})
        rc, out = self.render()
        self.assertEqual(rc, 3)
        self.assertIn("種類=読まない作業フォルダの名前", out)

    def test_old_client_skill_is_removed_from_org(self):
        d = os.path.join(self.client_app, ".claude", "skills", "globex-invoice")
        os.makedirs(d)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: globex-invoice\ndescription: x\n---\n")
        self.prepare({"company_name": "", "people": {"globex-invoice": {"dept": "accounting", "real_name_allowed": True}},
                      "letters": {"accounting": {"globex-invoice": "A"}}})
        self.render("--no-share")
        with open(os.path.join(self.out, "org.json"), encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("globex", text)

    def test_own_skill_with_the_same_name_as_a_client_skill_is_kept(self):
        self.h.add_skill("deploy")
        d = os.path.join(self.client_app, ".claude", "skills", "deploy")
        os.makedirs(d)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: deploy\ndescription: x\n---\n")
        os.makedirs(self.out, exist_ok=True)
        with open(os.path.join(self.out, "history.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"date": "2026-09-01", "employees": {"deploy": ["2026-08-01", "2026-08-30", 2]},
                                "employee_count": 1}) + "\n")
        self.prepare({"company_name": "", "people": {"deploy": {"dept": "accounting", "real_name_allowed": True}},
                      "letters": {"accounting": {"deploy": "A"}}})
        self.render("--no-share")
        with open(os.path.join(self.out, "org.json"), encoding="utf-8") as f:
            org = json.load(f)
        self.assertEqual(org["people"]["deploy"], {"dept": "accounting", "real_name_allowed": True})
        self.assertEqual(org["letters"]["accounting"]["deploy"], "A")
        with open(os.path.join(self.out, "history.jsonl"), encoding="utf-8") as f:
            self.assertIn("deploy", f.readline())  # 2026-09-01 の行に残っている

    def test_old_client_skill_is_removed_from_history(self):
        d = os.path.join(self.client_app, ".claude", "skills", "globex-invoice")
        os.makedirs(d)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: globex-invoice\ndescription: x\n---\n")
        os.makedirs(self.out, exist_ok=True)
        with open(os.path.join(self.out, "history.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"date": "2026-09-01", "employees": {"globex-invoice": ["2026-08-01", "2026-08-30", 2],
                                                                    "weekly-report": ["2026-08-01", "2026-08-30", 5]},
                                "employee_count": 2}) + "\n")
        self.prepare({"company_name": "", "people": {}})
        self.render("--no-share")
        with open(os.path.join(self.out, "history.jsonl"), encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("globex", text)
        self.assertIn("weekly-report", text)

    def test_letter_freed_by_removal_does_not_collide(self):
        org = render.normalize_org({"people": {"old": {"dept": "accounting"}, "x": {"dept": "accounting"},
                                               "new": {"dept": "accounting"}},
                                    "letters": {"accounting": {"x": "A", "old": "B"}}},
                                   {"skills": [{"id": "new", "kind": "own", "public": False, "joined": "2026-09-01"}],
                                    "retired": [], "agents": []}, VOCAB, drop={"x"})
        self.assertEqual(org["letters"]["accounting"], {"old": "B", "new": "A"})


if __name__ == "__main__":
    unittest.main()
