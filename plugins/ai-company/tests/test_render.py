import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import collect  # noqa: E402
import fakes  # noqa: E402
import render  # noqa: E402

VOCAB = render.load_vocab()
TODAY = fakes.TODAY
DEPT_IDS = [d["id"] for d in VOCAB["departments"]]


class Env:
    """架空の ~/.claude -> collect -> org.json -> render を一式で動かす。"""

    def __init__(self, tc, size="small", form="new", leaky=False, org=None, assign_depts=True):
        self.tmp = tempfile.TemporaryDirectory()
        tc.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.home = fakes.make_home(os.path.join(self.root, "home"), size, form, leaky=leaky)
        self.work = os.path.join(self.root, "ai-company", "work")
        self.out = os.path.join(self.root, "ai-company")
        self.company = collect.collect(self.home.claude, self.work, TODAY, history_path=os.path.join(self.out, "history.jsonl"), log=lambda *a: None)
        if org is None:
            org = {"company_name": "", "people": {}}
        if assign_depts:
            for i, s in enumerate(self.company["skills"]):
                org["people"].setdefault(s["id"], {"dept": DEPT_IDS[i % len(DEPT_IDS)], "real_name_allowed": False})
        with open(os.path.join(self.out, "org.json"), "w", encoding="utf-8") as f:
            json.dump(org, f, ensure_ascii=False)

    def run(self, *args):
        args = list(args)
        if "--approve" in args and (args.index("--approve") + 1 == len(args) or args[args.index("--approve") + 1].startswith("--")):
            # 本人に見せる回を先に流し、出た印で承認する（SKILL.md の手順4と同じ順）
            rest = [a for a in args if a != "--approve"]
            rc, out = self.run_raw(*rest)
            code = next((ln.split(": ", 1)[1] for ln in out.splitlines() if ln.startswith("APPROVE_CODE: ")), "none")
            args[args.index("--approve")] = "--approve=" + code
        return self.run_raw(*args)

    def run_raw(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = render.main(["--work-dir", self.work, "--out-dir", self.out] + list(args))
        return rc, buf.getvalue()

    def read(self, name):
        with open(os.path.join(self.out, name), encoding="utf-8") as f:
            return f.read()

    def exists(self, name):
        return os.path.exists(os.path.join(self.out, name))

    def org(self):
        return json.load(open(os.path.join(self.out, "org.json"), encoding="utf-8"))


def banned_for_leaky(env):
    words = [fakes.CUSTOMER, "portal-app", "tester-org", "my-helper",
             os.path.basename(env.root), os.path.basename(env.tmp.name)]
    words += [s["id"] for s in env.company["skills"] if s["kind"] == "own"]
    words += [a["id"] for a in env.company["agents"] if a.get("own")]
    return words


class Approval(unittest.TestCase):
    """承認は、本人に見せた一覧と同じ中身のときだけ通る（取り込んだ文の指示で勝手に進まないように）。"""

    def test_approve_without_shown_code_does_not_write(self):
        env = Env(self)
        rc, out = env.run_raw("--approve=0123456789ab")
        self.assertEqual(rc, 0)
        self.assertIn("STATUS: awaiting-approval", out)
        self.assertFalse(env.exists("share.html"))

    def test_change_after_showing_needs_showing_again(self):
        env = Env(self)
        _, out = env.run_raw()
        code = next(ln.split(": ", 1)[1] for ln in out.splitlines() if ln.startswith("APPROVE_CODE: "))
        org = env.org()
        sid = next(s["id"] for s in env.company["skills"] if not s["public"])
        org["people"][sid]["real_name_allowed"] = True  # 見せたあとで実名の許可が足された
        with open(os.path.join(env.out, "org.json"), "w", encoding="utf-8") as f:
            json.dump(org, f, ensure_ascii=False)
        rc, out = env.run_raw("--approve=" + code)
        self.assertIn("中身が変わりました", out)
        self.assertFalse(env.exists("share.html"))
        rc, out = env.run("--approve")
        self.assertIn("STATUS: done", out)

    def test_typed_text_is_removed_by_render(self):
        env = Env(self)
        self.assertTrue(os.path.exists(os.path.join(env.work, "typed.txt")))
        env.run()
        self.assertFalse(os.path.exists(os.path.join(env.work, "typed.txt")))


class LeakTests(unittest.TestCase):
    """最重要: 顧客名・パス・自作スキル名・自作サブエージェント名が外に出す版に残らない。"""

    def test_nothing_leaks_into_share_but_portal_has_real_names(self):
        for size in ("small", "medium"):
            env = Env(self, size, leaky=True)
            rc, out = env.run("--approve")
            self.assertEqual(rc, 0, out)
            share = env.read("share.html").lower()
            for w in banned_for_leaky(env):
                self.assertNotIn(w.lower(), share, "share.html に %r が残っている" % w)
            portal = env.read("portal.html")
            self.assertIn("globex-invoice", portal)
            self.assertIn("この版は実名入りです", portal)

    def test_description_text_not_in_share(self):
        env = Env(self, "small", leaky=True)
        env.run("--approve")
        self.assertNotIn("Globex 商事", env.read("share.html"))
        self.assertNotIn("Globex 商事", env.read("portal.html"))  # 説明文は画面に出さない

    def test_company_name_with_customer_word_stops_and_writes_no_share(self):
        env = Env(self, "small", leaky=True, org={"company_name": "Globex 商事の AI 会社", "people": {}})
        open(os.path.join(env.out, "share.html"), "w").write("old")  # 前回の分
        for args in ([], ["--approve"]):
            rc, out = env.run(*args)
            self.assertEqual(rc, 3, out)
            self.assertIn("STATUS: leak", out)
            self.assertIn("LEAK:", out)
            self.assertIn("会社名", out)  # 場所が出る
            self.assertFalse(env.exists("share.html"))  # 前回の分も消えている
        self.assertTrue(env.exists("portal.html"))  # 手元の版は作る

    def test_hint_when_leak_word_is_in_own_company_name(self):
        env = Env(self, "small", leaky=True, org={"company_name": "Globex 商事の AI 会社", "people": {}})
        rc, out = env.run()
        self.assertEqual(rc, 3)
        self.assertIn("ヒント: globex はあなたが付けた会社名にも入っています", out)

    def test_leak_words_not_written_to_files_other_than_user_inputs(self):
        env = Env(self, "small", leaky=True, org={"company_name": "Globex 商事の AI 会社", "people": {}})
        env.run("--approve")
        for name in os.listdir(env.out):
            path = os.path.join(env.out, name)
            if os.path.isfile(path) and name not in ("org.json", "portal.html", "history.jsonl"):
                self.assertNotIn("LEAK", open(path, encoding="utf-8").read())

    def test_resolve_mask_rebuilds_without_the_word(self):
        env = Env(self, "small", leaky=True, org={"company_name": "Globex 商事の AI 会社", "people": {}})
        rc, out = env.run("--approve", "--resolve", "mask")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("globex", env.read("share.html").lower())
        self.assertNotIn("globex", env.org()["company_name"].lower())
        self.assertIn("■", env.org()["company_name"])

    def test_resolve_allow_keeps_word_and_remembers_choice(self):
        env = Env(self, "small", leaky=True, org={"company_name": "Globex 商事の AI 会社", "people": {}})
        rc, out = env.run("--approve", "--resolve", "allow")
        self.assertEqual(rc, 0, out)
        self.assertIn("Globex", env.read("share.html"))
        self.assertIn("globex", env.org()["allowed_terms"])
        rc2, _ = env.run("--approve")  # 2回目は聞かれない
        self.assertEqual(rc2, 0)

    def test_values_outside_the_fixed_lists_never_reach_output(self):
        org = {"company_name": "", "people": {}, "jobs": [
            {"work": "globex請求書の作成", "count": 200, "covered": False},
            {"work": "invoice", "count": 50, "covered": False}]}
        env = Env(self, "small", leaky=True, org=org, assign_depts=False)
        first = env.company["skills"][0]["id"]
        env_org = env.org()
        env_org["people"] = {first: {"dept": "globex-dept", "real_name_allowed": False}}
        json.dump(env_org, open(os.path.join(env.out, "org.json"), "w"))
        rc, out = env.run("--approve")
        self.assertEqual(rc, 0, out)
        for name in ("share.html", "portal.html"):
            self.assertNotIn("globex請求書", env.read(name))
            self.assertNotIn("globex-dept", env.read(name))
        self.assertIn("請求書の作成", env.read("share.html"))  # 一覧にある方は載る
        self.assertEqual(env.org()["people"][first]["dept"], "general")

    def test_allowed_real_name_appears_only_when_allowed(self):
        env = Env(self, "small", leaky=True)
        o = env.org()
        o["people"]["weekly-report"]["real_name_allowed"] = True
        json.dump(o, open(os.path.join(env.out, "org.json"), "w"))
        rc, out = env.run("--approve")
        self.assertEqual(rc, 0, out)
        share = env.read("share.html")
        self.assertIn("weekly-report", share)
        self.assertNotIn("invoice-maker", share)
        self.assertNotIn("globex", share.lower())

    def test_awaiting_approval_writes_no_share(self):
        env = Env(self, "small")
        rc, out = env.run()
        self.assertEqual(rc, 0)
        self.assertIn("STATUS: awaiting-approval", out)
        self.assertFalse(env.exists("share.html"))
        self.assertIn("仮の職名", out)

    def test_no_share_mode(self):
        env = Env(self, "small")
        rc, out = env.run("--no-share")
        self.assertEqual(rc, 0)
        self.assertFalse(env.exists("share.html"))
        self.assertTrue(env.exists("portal.html"))

    def test_scan_catches_word_in_svg_text_and_attributes(self):
        env = Env(self, "small", leaky=True)
        org = render.normalize_org(env.org(), env.company, VOCAB)
        bad = '<svg><text>x portal-app y</text></svg><p aria-label="tester-org">z</p>'
        hits = render.scan_leaks(bad, env.company, org, VOCAB)
        words = {t for _, t, _ in hits}
        self.assertTrue({"portal-app", "tester-org"} <= words, words)

    def test_scripts_and_style_are_not_scanned(self):
        env = Env(self, "small", leaky=True)
        org = render.normalize_org(env.org(), env.company, VOCAB)
        hits = render.scan_leaks("<style>.globex{}</style><script>var globex=1</script><p>ok</p>", env.company, org, VOCAB)
        self.assertEqual(hits, [])


class NoFalseStops(unittest.TestCase):
    """0人・3人・30人・300人の架空の会社で、誤って止まらない。"""

    def test_every_size_and_form_completes(self):
        for size in ("tiny", "small", "medium", "large"):
            for form in ("new", "old"):
                for leaky in (False, True):
                    env = Env(self, size, form=form, leaky=leaky)
                    rc, out = env.run("--approve")
                    self.assertEqual(rc, 0, "%s/%s/%s\n%s" % (size, form, leaky, out))
                    self.assertTrue(env.exists("share.html"))
                    self.assertTrue(env.exists("portal.html"))

    def test_generic_path_words_do_not_stop(self):
        env = Env(self, "small")
        env.company["seeds"]["paths"] += ["home", "users", "docs", "work", "project", "khara"]
        json.dump(env.company, open(os.path.join(env.work, "company.json"), "w"))
        rc, out = env.run("--approve")
        self.assertEqual(rc, 0, out)

    def test_term_matching_a_fixed_word_is_dropped(self):
        env = Env(self, "small")
        env.company["seeds"]["paths"] += ["経理部", "claude-plugins-official"]
        json.dump(env.company, open(os.path.join(env.work, "company.json"), "w"))
        rc, out = env.run("--approve")
        self.assertEqual(rc, 0, out)

    def test_missing_company_json(self):
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as t, contextlib.redirect_stderr(buf):
            rc = render.main(["--work-dir", t, "--out-dir", t])
        self.assertEqual(rc, 2)


class Departments(unittest.TestCase):
    def test_at_most_seven_departments_rest_become_other(self):
        env = Env(self, "medium")  # 12部署に散らばる
        rc, out = env.run("--approve")
        self.assertEqual(rc, 0, out)
        org = render.normalize_org(env.org(), env.company, VOCAB)
        m = render.build_model(env.company, org, VOCAB)
        self.assertEqual(len(m["dept_rows"]), 7)
        self.assertEqual(m["dept_rows"][-1][1], "その他")
        self.assertEqual(sum(c for _, _, c in m["dept_rows"]), len(m["emps"]))
        names = [n for _, n, _ in m["dept_rows"][:-1]]
        self.assertEqual(len(set(names)), 6)

    def test_few_employees_only_departments_with_people(self):
        env = Env(self, "small")
        org = render.normalize_org(env.org(), env.company, VOCAB)
        m = render.build_model(env.company, org, VOCAB)
        self.assertEqual(len(m["dept_rows"]), len({e["dept"] for e in m["emps"]}))
        self.assertLessEqual(len(m["dept_rows"]), len(m["emps"]))
        self.assertLessEqual(len(m["top"]), len(m["emps"]))


class Titles(unittest.TestCase):
    def test_letters(self):
        self.assertEqual([render.letters_for(i) for i in (0, 1, 25, 26, 27, 51, 52)],
                         ["A", "B", "Z", "AA", "AB", "AZ", "BA"])

    def test_300_in_one_department(self):
        company = {"today": "2026-10-08", "founded": "2026-01-01", "range": {}, "skills": [
            {"id": "s%03d" % i, "kind": "own", "public": False, "joined": "2026-02-01"} for i in range(300)],
            "agents": [], "retired": []}
        org = render.normalize_org({"people": {}}, company, VOCAB)
        letters = list(org["letters"]["general"].values())
        self.assertEqual(len(set(letters)), 300)
        self.assertIn("AA", letters)

    def test_retired_letter_is_not_reused_and_titles_are_stable(self):
        company = {"today": "2026-10-08", "founded": "2026-01-01", "range": {}, "agents": [], "retired": [],
                   "skills": [{"id": "new", "kind": "own", "public": False, "joined": "2026-09-01"}]}
        org = render.normalize_org({"people": {"old": {"dept": "accounting"}, "new": {"dept": "accounting"}},
                                    "letters": {"accounting": {"old": "A"}}}, company, VOCAB)
        self.assertEqual(org["letters"]["accounting"], {"old": "A", "new": "B"})
        again = render.normalize_org(org, company, VOCAB)
        self.assertEqual(again["letters"], org["letters"])


class CompanyWords(unittest.TestCase):
    """普通の人にも分かるように、区分・まとめ役・この会社の見方を出す（2026-10-21 オーナー指示）。"""

    def env_with_manager(self, manager):
        env = Env(self, "small")
        c = json.load(open(os.path.join(env.work, "company.json"), encoding="utf-8"))
        own = [s for s in c["skills"] if s["kind"] == "own"]
        own[0]["manager"] = manager
        json.dump(c, open(os.path.join(env.work, "company.json"), "w", encoding="utf-8"), ensure_ascii=False)
        return env, c

    def test_share_has_breakdown_glossary_and_hire_kind(self):
        env, c = self.env_with_manager(True)
        rc, out = env.run("--approve")
        self.assertIn("STATUS: done", out)
        html = env.read("share.html")
        own = sum(1 for s in c["skills"] if s["kind"] == "own")
        self.assertIn("%d名（自作 %d・中途入社 %d）" % (len(c["skills"]), own, len(c["skills"]) - own), html)
        self.assertIn("この会社の見方", html)
        self.assertIn("サブエージェント", html)
        self.assertIn("<dt>まとめ役</dt><dd>1名</dd>", html)
        self.assertIn("<dt>採用</dt><dd>中途入社（プラグイン）</dd>", html)
        self.assertIn("まとめ役 ・ 自作 2026-", html)
        self.assertIn("うち自作 %d名" % own, html)  # 画像の行

    def test_no_manager_row_when_none(self):
        env, _ = self.env_with_manager(False)
        env.run("--approve")
        self.assertNotRegex(env.read("share.html"), r"<dt>まとめ役</dt><dd>\d+名</dd>")  # 見方の説明の行は残る


class OrgPlace(unittest.TestCase):
    def test_org_written_in_work_folder_is_used_and_moved(self):
        """手順3で org.json を work/ に書いても、黙って無視しない（2026-10-21 試しで会社名が消えた）。"""
        env = Env(self, "small")
        os.remove(os.path.join(env.out, "org.json"))
        with open(os.path.join(env.work, "org.json"), "w", encoding="utf-8") as f:
            json.dump({"company_name": "エーワン研究所", "people": {}}, f, ensure_ascii=False)
        rc, out = env.run()
        self.assertIn("org.json が", out)
        self.assertEqual(env.org()["company_name"], "エーワン研究所")
        self.assertFalse(os.path.exists(os.path.join(env.work, "org.json")))
        self.assertIn("[会社名] エーワン研究所", out)


class OrgPlaceMore(unittest.TestCase):
    def test_newer_org_in_work_folder_stops(self):
        """2回目以降に work/ にまた書かれたら、古いほうで作り続けず止まる（レビューの指摘）。"""
        env = Env(self, "small")
        p = os.path.join(env.work, "org.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"company_name": "新しい名前", "people": {}}, f, ensure_ascii=False)
        os.utime(p, (os.path.getmtime(os.path.join(env.out, "org.json")) + 10,) * 2)
        rc, out = env.run_raw()
        self.assertEqual(rc, 2)
        self.assertIn("STATUS: error", out)
        self.assertFalse(os.path.exists(os.path.join(env.work, "typed.txt")))  # 止まっても打った文は残さない

    def test_folder_named_org_json_is_not_moved(self):
        env = Env(self, "small")
        os.makedirs(os.path.join(env.work, "org.json"))
        rc, out = env.run()
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isfile(os.path.join(env.out, "org.json")))


class Jobs(unittest.TestCase):
    def model(self, jobs, total, sample, extra=None):
        company = {"today": "2026-10-08", "founded": "2026-06-01", "range": {"oldest": "2026-08-08", "newest": "2026-10-08"},
                   "stats": {"files": 1}, "skills": [], "agents": [], "retired": [],
                   "typed": {"total_30d": total, "sample_n": sample, "found": True}}
        org = render.normalize_org({"jobs": jobs}, company, VOCAB)
        return render.build_model(company, org, VOCAB)

    def test_estimate_from_sample_and_threshold(self):
        m = self.model([{"work": "invoice", "count": 12}, {"work": "expense", "count": 2},
                        {"work": "report", "count": 40, "covered": True}], 600, 300)
        self.assertEqual([(j["work"], j["est"], j["sampled"]) for j in m["jobs"]], [("請求書の作成", 24, True)])

    def test_no_sampling_when_300_or_fewer(self):
        m = self.model([{"work": "invoice", "count": 7}], 120, 120)
        self.assertEqual((m["jobs"][0]["est"], m["jobs"][0]["sampled"]), (7, False))

    def test_small_sample_is_not_stretched(self):
        """抜き出しが少ないときは全件へ引き伸ばさず「少なくとも」と出す（2026-10-21 試しで 2件中1件が約5回になった）。"""
        m = self.model([{"work": "invoice", "count": 1}, {"work": "expense", "count": 6}], 10, 8)
        self.assertEqual([(j["work"], j["est"], j["prefix"]) for j in m["jobs"]], [("経費の整理", 6, "少なくとも")])
        self.assertTrue(m["small_sample"])
        m = self.model([{"work": "invoice", "count": 12}], 600, 300)
        self.assertEqual((m["jobs"][0]["est"], m["jobs"][0]["prefix"]), (24, "約"))

    def test_top_three_and_other_excluded(self):
        jobs = [{"work": w, "count": c} for w, c in (("invoice", 9), ("expense", 8), ("report", 7), ("slides", 6), ("other", 99))]
        m = self.model(jobs, 100, 100)
        self.assertEqual(len(m["jobs"]), 3)
        self.assertEqual(m["jobs"][0]["work"], "請求書の作成")

    def test_zero_jobs_message(self):
        env = Env(self, "small")
        rc, _ = env.run("--approve")
        self.assertIn("今は求人はありません", env.read("share.html"))

    def test_job_page_has_copy_prompt_and_link(self):
        env = Env(self, "small")
        o = env.org()
        o["jobs"] = [{"work": "invoice", "count": 20, "covered": False}]
        json.dump(o, open(os.path.join(env.out, "org.json"), "w"))
        env.run("--approve")
        html = env.read("share.html")
        self.assertIn("経理担当", html)
        self.assertIn("請求書の作成", html)
        self.assertIn("依頼文をコピー", html)
        self.assertIn("code.claude.com/docs/en/skills", html)


class Image(unittest.TestCase):
    def full_model(self, name="あ" * 60, emps=47):
        skills = [{"id": "s%03d" % i, "kind": "own", "public": False, "joined": "2026-06-19", "last_used": "2026-10-07",
                   "count_30d": 300 - i, "count_total": 500} for i in range(emps)]
        company = {"today": "2026-10-08", "founded": "2026-06-19", "range": {"oldest": "2026-08-08", "newest": "2026-10-08"},
                   "stats": {"files": 3}, "skills": skills, "agents": [], "retired": [],
                   "typed": {"total_30d": 900, "sample_n": 300, "found": True}}
        org = {"company_name": name, "people": {s["id"]: {"dept": DEPT_IDS[i % 12]} for i, s in enumerate(skills)},
               "jobs": [{"work": "invoice", "count": 30}]}
        org = render.normalize_org(org, company, VOCAB)
        return render.build_model(company, org, VOCAB)

    def test_fits_in_canvas_in_worst_case(self):
        m = self.full_model()
        svg, bottom = render.build_svg(m)
        self.assertLess(bottom, 1320)
        self.assertEqual(svg.count("<circle"), 2)  # 印鑑は2つまで（在籍・募集）
        self.assertNotIn("foreignObject", svg)
        self.assertIn('viewBox="0 0 1080 1350"', svg)

    def test_no_job_means_no_recruit_seal(self):
        m = self.full_model()
        m["jobs"] = []
        svg, _ = render.build_svg(m)
        self.assertEqual(svg.count("<circle"), 1)
        self.assertNotIn("募集", svg)

    def test_zero_employees_image(self):
        m = self.full_model(emps=0)
        svg, bottom = render.build_svg(m)
        self.assertIn("創業準備中", svg)
        self.assertNotIn("部署", svg)
        self.assertNotIn("在籍", svg)
        self.assertLess(bottom, 1320)
        self.assertIn("はじめの3歩", svg)
        self.assertEqual(bottom, 1350 - 72)  # 末尾の表記が下端にあり、下半分が空かない

    def test_html_shows_each_doc_number_once(self):
        m = self.full_model()
        html = render.build_html(m, "share", render.build_svg(m)[0])
        self.assertEqual(html.count("AIC-HOME-001"), 1)

    def test_alt_text_summarises(self):
        m = self.full_model("会社")
        alt = render.alt_text(m, "share")
        self.assertIn("社員47名", alt)
        self.assertIn("求人は", alt)

    def test_share_html_has_save_button_and_portal_does_not(self):
        env = Env(self, "small")
        env.run("--approve")
        self.assertIn('id="save-img"', env.read("share.html"))
        self.assertIn("画像の説明文をコピー", env.read("share.html"))
        self.assertNotIn("save-img", env.read("portal.html"))
        self.assertNotIn("foreignObject", env.read("share.html"))
        self.assertIn("自作スキルの名前は仮の職名に置き換えています", env.read("share.html"))

    def test_font_stack_is_os_fonts_only(self):
        env = Env(self, "small")
        env.run("--approve")
        h = env.read("share.html")
        self.assertNotIn("fonts.googleapis", h)
        self.assertNotIn("http://", h.replace("http://www.w3.org/2000/svg", ""))


class Portal(unittest.TestCase):
    def test_structure_and_accessibility(self):
        env = Env(self, "medium")
        env.run("--approve")
        h = env.read("portal.html")
        self.assertIn('<html lang="ja">', h)
        for sid in ("overview", "org", "roster", "history", "jobs"):
            self.assertIn('id="%s"' % sid, h)
        self.assertIn("<header>", h)
        self.assertIn("<main>", h)
        self.assertIn('aria-label="目次"', h)
        self.assertIn("prefers-reduced-motion", h)
        self.assertIn("ほか", h)  # 1部署10人超の折りたたみ（medium では出ない場合もあるので下で large を確認）

    def test_roster_folds_over_ten(self):
        env = Env(self, "large")
        o = env.org()
        for s in o["people"]:
            o["people"][s]["dept"] = "accounting"
        json.dump(o, open(os.path.join(env.out, "org.json"), "w"))
        env.run("--approve")
        self.assertRegex(env.read("share.html"), "ほか\\d+名を表示")

    def test_empty_company_states(self):
        env = Env(self, "tiny")
        env.run("--approve")
        h = env.read("share.html")
        self.assertIn("まだ社員がいません", h)
        self.assertIn("会話の記録がありません", h)
        self.assertIn("依頼文をコピー", h)

    def test_leave_status_after_30_days(self):
        company = {"today": "2026-10-08", "founded": "2026-01-01", "range": {"oldest": "2026-01-01", "newest": "2026-10-08"},
                   "stats": {"files": 1}, "agents": [], "retired": [],
                   "skills": [{"id": "idle", "kind": "own", "public": False, "joined": "2026-02-01", "last_used": "2026-08-01",
                               "count_30d": 0, "count_total": 3}]}
        org = render.normalize_org({"people": {}}, company, VOCAB)
        m = render.build_model(company, org, VOCAB)
        self.assertEqual(m["emps"][0]["status"], "休職")
        kinds = [k for p in m["timeline"] for _, k, _ in p["events"]]
        self.assertIn("休職", kinds)
        self.assertIn("入社", kinds)
        self.assertIn("創業", kinds)

    def test_period_header_counts(self):
        company = {"today": "2026-10-08", "founded": "2026-06-19", "range": {}, "stats": {}, "agents": [], "retired": [
            {"id": "gone", "joined": "2026-06-19", "retired_on": "2026-09-01"}],
            "skills": [{"id": "s%d" % i, "kind": "own", "public": False, "joined": "2026-07-01"} for i in range(3)]}
        org = render.normalize_org({"people": {}}, company, VOCAB)
        m = render.build_model(company, org, VOCAB)
        p = m["timeline"][0]
        self.assertEqual((p["n"], p["from"], p["to"]), (1, 1, 3))

    def test_recorded_range_band_and_previous_records_note(self):
        env = Env(self, "small")
        env.run("--approve")
        self.assertRegex(env.read("share.html"), "記録は 2026-0\\d-\\d\\d 以降")
        # 2回目: 前回の記録が今の記録より古いとき
        env.company["history_range"] = {"oldest": "2026-06-01", "rows": 1}
        json.dump(env.company, open(os.path.join(env.work, "company.json"), "w"))
        env.run("--approve")
        self.assertIn("までは前回までの記録から", env.read("share.html"))


class History(unittest.TestCase):
    def test_one_row_per_run_date_and_no_typed_text(self):
        env = Env(self, "small")
        env.run("--approve")
        env.run("--approve")
        rows = [json.loads(ln) for ln in env.read("history.jsonl").splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], TODAY.isoformat())
        self.assertEqual(rows[0]["employee_count"], len(env.company["skills"]))
        for s in fakes.TYPED_SAMPLES:
            self.assertNotIn(s, env.read("history.jsonl"))

    def test_history_makes_roster_grow_after_records_expire(self):
        env = Env(self, "small")
        env.run("--approve")
        # 記録が消えた状態（会話の記録なし）を再現
        import shutil
        shutil.rmtree(os.path.join(env.home.claude, "projects"))
        os.makedirs(os.path.join(env.home.claude, "projects"))
        c2 = collect.collect(env.home.claude, env.work, TODAY + timedelta(days=40),
                             history_path=os.path.join(env.out, "history.jsonl"), log=lambda *a: None)
        self.assertEqual(c2["stats"]["files"], 0)
        first = next(s for s in c2["skills"] if s["id"] == "invoice-maker")
        self.assertEqual(first["joined"], "2026-06-19")
        self.assertIsNotNone(first["last_used"])  # 履歴から


class OrgJsonOutputsOnly(unittest.TestCase):
    def test_org_written_back_with_letters(self):
        env = Env(self, "small")
        env.run("--no-share")
        letters = env.org()["letters"]
        self.assertTrue(letters)


class ConcreteJobs(unittest.TestCase):
    """求人票に、具体的に何を頼んでいるかと打った文の例を出す（2026-10-24 オーナー「中身が分かりづらい」）。"""

    JOBS = [{"work": "todo_mgmt", "count": 30, "covered": False, "detail": "朝にその日の TODO を読んで並べ替える",
             "examples": ["今日の TODO を見せて", "終わったものに印を付けて", "期限の近い順に並べて", "4件目は捨てる"]},
            {"work": "learning", "count": 90, "covered": False, "detail": "用語やエラーの意味を聞く",
             "examples": ["この用語の意味は？"]}]

    def env(self):
        return Env(self, "small", org={"company_name": "", "people": {}, "jobs": [dict(j) for j in self.JOBS]})

    def test_local_version_shows_detail_and_examples_and_share_does_not(self):
        env = self.env()
        rc, out = env.run("--approve")
        self.assertEqual(rc, 0, out)
        portal, share = env.read("portal.html"), env.read("share.html")
        self.assertIn("朝にその日の TODO を読んで並べ替える", portal)
        self.assertIn("今日の TODO を見せて", portal)
        self.assertNotIn("4件目は捨てる", portal)  # 例は3件まで
        self.assertIn("たとえば次のように頼んでいます", portal)  # 依頼文にも例が入る
        for text in ("朝にその日の TODO", "今日の TODO を見せて", "たとえば次のように"):
            self.assertNotIn(text, share)
        self.assertIn("タスクの整理", share)

    def test_questions_are_not_a_job(self):
        env = self.env()
        env.run("--no-share")
        self.assertNotIn("学習・勉強の補助", env.read("portal.html").split('id="jobs"')[1])

    def test_portal_and_org_are_readable_only_by_the_owner(self):
        env = self.env()
        env.run("--no-share")
        for name in ("portal.html", "org.json"):
            self.assertEqual(os.stat(os.path.join(env.out, name)).st_mode & 0o777, 0o600, name)


if __name__ == "__main__":
    unittest.main()
