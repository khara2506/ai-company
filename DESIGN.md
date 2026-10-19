---
name: ai-company
description: 日本の会社の書類を、スマホで。罫線と余白で区切り、紺を主に、琥珀は注意、朱は印鑑だけ。
colors:
  paper: "#fffdf8"
  sheet: "#f7f2e7"
  rule: "#e4dccb"
  rule-strong: "#cfc4ad"
  text: "#1c1917"
  text-muted: "#57534e"
  text-faint: "#8a837a"
  primary: "#1e3a8a"
  on-primary: "#ffffff"
  accent: "#d97706"
  accent-text: "#b45f06"
  accent-tint: "#fdf1dc"
  seal: "#b4232a"
  info-tint: "#e8edf8"
  dark-paper: "#14120f"
  dark-sheet: "#1d1a16"
  dark-rule: "#34302a"
  dark-rule-strong: "#4a443b"
  dark-text: "#ece6da"
  dark-text-muted: "#b3ac9f"
  dark-text-faint: "#8a8478"
  dark-primary: "#9db2ec"
  dark-on-primary: "#0f1730"
  dark-accent: "#f0a43a"
  dark-accent-tint: "#3a2a12"
  dark-seal: "#e0605a"
typography:
  title: { fontFamily: "Noto Sans JP", fontWeight: 700, fontSize: "clamp(24px, 6vw, 32px)", lineHeight: 1.35 }
  heading: { fontFamily: "Noto Sans JP", fontWeight: 700, fontSize: 22px, lineHeight: 1.4 }
  body: { fontFamily: "Noto Sans JP", fontWeight: 400, fontSize: 16px, lineHeight: 1.75 }
  small: { fontSize: 13px, lineHeight: 1.5 }
  docline: { fontSize: 12px, letterSpacing: 0.02em }
rounded: { sm: 4px, md: 8px, lg: 12px }
spacing: { xs: 4px, sm: 8px, md: 16px, lg: 24px, xl: 32px, 2xl: 56px }
---

# ai-company の画面

## 方向

「日本の会社の書類を、スマホで」。頭書き（文書番号・創業日・第N期）、罫線と余白の区切り、社員証、朱の印鑑、紺と生成り。数字の表を並べる利用状況の画面とは見た目で区別できる。自分の AI 会社を、会社案内の1枚の書類として見せる。

## 色

- 紺が主役。押せるもの（リンク・ボタン・選択中のタブ）は紺
- 琥珀は「注意」だけ（手元の版の「実名入りです」の帯、キーボードで選んだときの枠線 3px）
- 朱は印鑑だけ。朱の丸い枠と文字のみ（塗りつぶさない）。1画面に2か所まで（画像では「在籍」「募集」）
- 画像は明るい紙のみ。ポータルは OS の設定に従って墨色の紙にもなる（紺と琥珀を明るめに寄せて同じ強さに見せる）

## 書体

本文から数字まで1種類。OS に入っている日本語の書体を順に指定する（外部の書体は読み込まない。オフラインで動き、SVG を画像にするときにも外部書体が読み込まれないため）。

`"Noto Sans JP","Hiragino Sans","Yu Gothic UI","Meiryo","Noto Sans CJK JP",sans-serif`

段階: 題 24〜32px ／ 見出し 22px ／ 本文 16px ／ 補足 13px ／ 頭書き 12px。表の数字は `tabular-nums`。画像の文字は OS ごとに幅が違うので、1行の字数に15%の余裕を取って折り返す。

## 配置

- スマホ（〜719px）: 1段・左右16px。下に5つのタブ（概要・組織・名簿・社史・求人）。選択中は紺の太字と `aria-current`
- PC（720px〜）: 左に目次、本文は最大 720px
- 1つの HTML に5つの区画。動きは画面内の移動と、社員証・部署の開閉（`details`）だけ
- 押せる場所は高さ 44px 以上。余白は 4px 刻み

## 部品

- 書類の頭書き: `AIC-ORG-001 ／ 創業 YYYY-MM-DD ／ 第N期`。番号の頭は `AIC-`、部署の略号（HOME・ORG・ROS・HST・JOB）＋連番
- 一覧の行: 罫線で区切る細い行。右端に「›」
- 社員証: 生成りの面・罫線・12px の角丸。影は開いたときだけ
- 求人票: 紺の罫線の枠
- お知らせ: 薄い色の面と、左の線と文字ラベル（「注意」）
- ボタン: 主（紺の塗り）・副（紺の枠）。押した後の文言は動作を言う（「コピーしました」）
- 空・長い名前・失敗の状態をどの部品にも作る（「データなし」だけで終えない）

## しない

- カードの中にカード、どの箱にも同じ角丸と影
- 本文の中央寄せ、紫のグラデーション、絵文字の飾り
- 朱を印鑑以外に使う。印鑑を1画面に3つ以上押す
- 琥珀を飾りに使う

## 動き

必要最小限。`prefers-reduced-motion` のときは止める。
