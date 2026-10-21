"""読まない作業フォルダ（顧客の仕事など）。collect.py と render.py が使う。

指定は ~/.claude/ai-company/exclude.json（{"version": 1, "dirs": ["~/clients"]}）と collect.py の --exclude。
org.json と分けているのは、手順3で Claude が org.json を書き直すときの巻き添えにしないため。
"""
import json
import os
import re
import sys

CASE_INSENSITIVE_FS = sys.platform in ("darwin", "win32")
FILE_NAME = "exclude.json"
WALK_DEPTH = 4  # 読まないフォルダの下で .claude/skills を探す深さ（~/clients/<顧客>/<リポジトリ>/.claude/skills まで）


class ExcludeError(ValueError):
    pass


def folder_key(path):
    """Claude Code が会話の記録のフォルダ名にする形（英数字以外を - に）。"""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _fold(s):
    return s.casefold() if CASE_INSENSITIVE_FS else s


def _under(path, top):
    return path == top or path.startswith(top if top.endswith(os.sep) else top + os.sep)


class Excluder:
    """打ったままの形と symlink を解いた形の両方で照合する（どちらの形で Claude Code を起動したか分からないため）。"""

    def __init__(self, dirs=()):
        self.dirs = []
        tops = set()
        for raw in dirs:
            plain = os.path.normpath(os.path.abspath(os.path.expanduser(raw)))
            if plain not in self.dirs:
                self.dirs.append(plain)
            tops |= {plain, os.path.realpath(plain)}
        self.tops = sorted(tops)
        self._forms = sorted({_fold(t) for t in tops})
        self._keys = sorted({_fold(folder_key(t)) for t in tops})
        self._cache = {}
        self.folders = self.files = self.lines = 0

    def __bool__(self):
        return bool(self.dirs)

    def missing(self):
        return [d for d in self.dirs if not os.path.isdir(d)]

    def covers_folder(self, name):
        """会話の記録のフォルダ名が、読まないフォルダ（とその下）のものか。~/clients-old のような似た名前も外れる（安全側）。"""
        n = _fold(name)
        return any(not k.strip("-") or n == k or n.startswith(k + "-") for k in self._keys)

    def covers(self, path):
        hit = self._cache.get(path)
        if hit is None:
            cands = {_fold(os.path.normpath(path)), _fold(os.path.realpath(path))}
            hit = any(_under(c, t) for c in cands for t in self._forms)
            self._cache[path] = hit
        return hit

    def skill_names(self):
        """読まないフォルダの下にある .claude/skills のスキルの名前。前に数えた分を退職として出さないため（ファイルには書かない）。"""
        names = set()
        for top in self.tops:
            if top == os.sep or not os.path.isdir(top):
                continue
            base = top.rstrip(os.sep).count(os.sep)
            for root, dirs, _ in os.walk(top):
                if os.path.basename(root) == ".claude":
                    dirs[:] = []
                    skills = os.path.join(root, "skills")
                    for entry in (os.listdir(skills) if os.path.isdir(skills) else []):
                        names.add(entry)
                        name = _front_name(os.path.join(skills, entry, "SKILL.md"))
                        if name:
                            names.add(name)
                    continue
                if root.count(os.sep) - base >= WALK_DEPTH:
                    dirs[:] = []
                else:
                    dirs[:] = [d for d in dirs if d == ".claude" or not (d.startswith(".") or d == "node_modules")]
        return names

    def folder_names(self):
        """読まないフォルダの直下のフォルダ名（顧客名のことが多い）。外に出す版の照合に使う（ファイルには書かない）。"""
        names = set()
        for top in self.tops:
            if top == os.sep or not os.path.isdir(top):
                continue
            try:
                entries = os.listdir(top)
            except OSError:
                continue
            names |= {e for e in entries if not e.startswith(".") and os.path.isdir(os.path.join(top, e))}
        return names


def _front_name(md):
    try:
        with open(md, encoding="utf-8", errors="replace") as f:
            head = f.read(4000)
    except OSError:
        return None
    m = re.search(r"^name:\s*(.+?)\s*$", head, re.M) if head.startswith("---") else None
    return m.group(1).strip("\"'") if m else None


def load(path, extra=()):
    """exclude.json が「ある・読めない」「dirs が文字列の一覧でない」ときは止まる。黙って全部読まないため。"""
    dirs = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                v = json.load(f)
        except (OSError, ValueError) as e:
            raise ExcludeError("%s を読めません（%s）" % (path, e))
        d = v.get("dirs") if isinstance(v, dict) else None
        if not isinstance(d, list) or not all(isinstance(x, str) and x.strip() for x in d):
            raise ExcludeError('%s の "dirs" はフォルダのパスの一覧にしてください（例 {"version": 1, "dirs": ["~/clients"]}）' % path)
        dirs.extend(x.strip() for x in d)
    dirs.extend(x for x in extra if x.strip())
    return Excluder(dirs)
