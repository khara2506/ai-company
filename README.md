# ai-company

自分の Claude Code の使い方を、「AI 会社」の組織図・社員名簿・社史・求人票にして見せる Claude Code プラグイン。X に貼る画像も1枚できます。

- スキル（自作・入れたスキル集・プラグイン）= 社員。サブエージェントと、Claude Code に最初から入っているスキル（`/code-review` など）= 外部スタッフ
  - 数えるのは `~/.claude/skills`、会話の記録に残っている作業フォルダ（とその上）の `.claude/skills`、プラグインのスキル。このプラグイン自身は数えません
  - 自作か他人の作かは、SKILL.md の実体（symlink の先）の git の作者にあなた（`user.email`）がいるか、`npx skills` の記録（`.skill-lock.json`・`skills-lock.json`）にあるかで見分けます。他人の作を自分のリポジトリにコピーしてコミットしたものは、自作に数えます
- 最初の会話の日 = 創業日、スキルが初めて呼ばれた日 = 入社日、30日呼ばれていない = 休職
- 手で何度も頼んでいるのにスキルになっていない作業を「求人」として出します。次に何を自動化するかが分かります

## 使い方

```
/plugin marketplace add khara2506/ai-company
/plugin install ai-company@ai-company
/ai-company
```

初回は会社名と、実名で外に出してよいスキルを聞かれます（何も答えなければ全員「経理担当A」のような仮の職名）。

出るもの（`~/.claude/ai-company/`）:

| ファイル | 中身 | 用途 |
|---|---|---|
| `portal.html` | 実名入りの手元の版 | 自分で見る。共有しない |
| `share.html` | 外に出す版（確認を通ったものだけ） | 人に見せる・画像を保存する |
| `history.jsonl` | 実行ごとの記録（会話の文は入らない） | 記録が消えた後も社史を伸ばす |
| `org.json` | 会社名・部署・職名・許可 | 2回目以降も同じ職名にする |

## 何をどこへ送るか

- このプラグイン独自のサーバーは無く、**どこにも送りません**。公開スキルかどうかを調べるための外への問い合わせもしません
- ただし手順2は Claude が判断するので、普段の Claude Code と同じく、渡した材料は Anthropic のモデルに送られます。渡すのは次の2つです
  1. スキルの名前と説明文（部署分け・職名のため。作業フォルダの `.claude/skills` の分も含みます）
  2. あなたが打った文の抜き出し（この30日・1件200字・最大300件・合計3万字以内。求人票のため）
- 会話の記録をまるごと渡すことはしません。ファイルの中身やツールの結果は読みません
- 外に出す版（`share.html`）に載るのは、決まった部署名・「経理担当A」形式の職名・公開スキル（公式のプラグインと、`vocab.json` の公開の一覧にある配布元から入れたスキル集）の実名・数・日付・求人票の型の文だけです。さらに、手元で集めた固有の語（自作スキル名・作業フォルダのパス・git のリモート名・あなたの名前）が残っていないかを照合し、残っていれば書き出さずに止まります。最後に、載る文字列の一覧を見せて、OK を押してから書き出します

## 対応環境

- Mac・Linux・WSL。Python 3.9 以上（標準ライブラリだけ。`python3` → `python` の順で探します）
- **Windows のネイティブでは動かしません**（対象外）。WSL で使ってください

## 仕組み

数えるのと画面を作るのは決まった処理（同じ記録からは同じ結果）、判断だけを Claude に任せます。

```
plugins/ai-company/
  skills/ai-company/SKILL.md   /ai-company の手順書
  scripts/collect.py           ~/.claude と git を読んで数える
  scripts/render.py            HTML と画像（SVG）を作る・禁止語の照合
  scripts/vocab.json           部署12・作業の一覧・一般語（外に出す版の守りの土台）
  tests/                       架空の ~/.claude で試す
```

会話の記録は Claude Code が既定で30日で消します（`cleanupPeriodDays`）。このプラグインは実行のたびに結果を `history.jsonl` に貯めるので、2回目以降は消えた分も社史に残ります。

## 開発

```
cd plugins/ai-company
python3 -m unittest discover -s tests
python3 tests/fakes.py /tmp/fake medium new     # 架空の ~/.claude を作る（tiny / small / medium / large、new / old、--leaky）
python3 scripts/collect.py --claude-dir /tmp/fake/.claude --out-dir /tmp/out/work
python3 scripts/render.py --work-dir /tmp/out/work --out-dir /tmp/out
```

出力先は引数で変えられます。実際の `~/.claude` は読むだけで、書き込みません。

## 費用

手順2で Claude が読む量（自作スキルの説明文と抜き出し最大3万字）のトークン数と費用は、未確定です（実機で測って追記します）。

## ライセンス

MIT
