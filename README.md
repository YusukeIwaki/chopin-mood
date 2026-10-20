# Piano × AI Animation — GitHub Studio（初期実装）

更新日: 2026-10-20 / 言語: 日本語

mainへの導入後、Actionsの「Studio auth smoke」でCodex/Muse両方の認証を確認してから使用する。
この公開リポジトリに添付した録音は公開アクセス可能になる。

## 現在の使い方（1〜2分、手作業のタイミング調整なし）

1. GitHub Issue フォームから本人のピアノ録音を **GitHub 添付として1件**
   アップロードする（曲名・時期・メモは任意、既定は unknown）。
2. Codex が DSP 候補（60〜120秒、最大3件）から ID で選び、子 Issue を作り、
   Muse が宣言的 storyboard を作り、信頼レンダラーが MP4 を書き出す。
3. Codex の証拠レビュー通過時だけ `@YusukeIwaki please check this` が付くので、
   確認して **Approve → Merge** すると、同一 artifact が Release に公開される。

詳細・運用・制限は [docs/10_GITHUB_STUDIO_OPERATIONS.md](docs/10_GITHUB_STUDIO_OPERATIONS.md)。
録音レート別の解析校正は [docs/11_SAMPLE_RATE_TUNING.md](docs/11_SAMPLE_RATE_TUNING.md)。
現状は手続き描画のプロトタイプであり完成アニメではない。音声の聴取による
音楽的検証は行われていない。手作業のタイムライン編集の受け口はない。

以下は P0 までの経緯・引き継ぎ記録（保持）。

---

# Piano × AI Animation — Codex 引き継ぎパッケージ

更新日: 2026-10-19 / 言語: 日本語 / 状態: 構想・技術検証の初期段階

## このプロジェクトで作りたいもの

ピアニスト本人の演奏音源を投入すると、音楽の盛り上がり・フレーズ・音色の変化を解析し、短いアニメーション作品を**原音源と自動同期**して書き出すシステム。主用途は YouTube Shorts（30〜60秒程度）。単なるオーディオビジュアライザーや AI 風景 BGM の量産ではなく、ショパンの印象的なフレーズを「演奏解釈のある短編映像」にする。

**最重要条件: ユーザーが毎回、カット位置・音符とのタイミング・動きの強さを手でチューニングする必要がないこと。**

初期はショパン中心だが、対象は曲名非依存に設計する。曲名が不明でも音声解析は動くこと。

## 最初に読む順番

1. `AGENTS.md` — Codex に守ってほしい方針
2. `prompts/CODEX_START.md` — Codex への最初の依頼文（コピペ可）
3. `docs/01_CONTEXT_AND_GOALS.md` — 企画背景、必須条件、事実と仮説
4. `docs/02_ARCHITECTURE.md` — 暫定アーキテクチャとデータ契約
5. `docs/03_AUDIO_AND_SYNC.md` — 音声特徴量・同期・評価の論点
6. `docs/04_POC_PLAN.md` — 最初の技術検証、Done 条件、テスト
7. `docs/05_CONTENT_AND_BUSINESS.md` — ファン獲得と収益化、動画案10本
8. `docs/06_RESEARCH_AND_RISKS.md` — 未検証事項、参考リンク、権利
9. `docs/09_ARCHITECTURE_DECISION_AND_ACCEPTANCE.md` — 技術方式比較・次段の受け入れ条件（親作成）

## 現在ある実データ

- `assets/reference_audio.wav` — 添付された縦型ピアノ演奏動画から以前の会話で抽出されたモノラル音声（約167.15秒、22,050Hz）。**音響検証専用で、公開動画の最終音源には使わない**。公開時は元動画の音声トラックまたは高品質な録音マスターを使用する。外部に無断アップロードしない。
- `assets/music_timeline.png` — 以前の解析の結果グラフ。参考指標であり、楽曲理解を実証するものではない。
- `examples/reference_features_v0.json` — 5秒ごとの特徴量と局所ピーク。以前の解析スクリプト由来。
- `scripts/analyze_reference.py` — 解析処理を再実行する参考スクリプト。初期の特徴量設計の一例であり製品用実装ではない。
- `assets/reference_contact_sheet.jpg` — 入力動画の抜粋フレーム（実演映像の可能性検証に利用）。
- **元動画 `IMG_0303.mp4` は約102MBのためZIPには含めていない**。元のチャット添付から別途作業リポジトリへ配置すること。必ずしも元動画がないと音響PoCに着手できないわけではない。

**注意**: 元の演奏曲名は資料だけでは確定していない。添付素材を「スケルツォ第3番」と決め打ちしないこと。以前の会話で出てきた同曲は、企画例・レパートリーの話として区別する。

## 前の会話で実測されたこと

- 元動画: 約167.1667秒、512×910、30fps、映像H.264・音声AAC。
- 音声特徴量には RMS、onset strength、spectral centroid、合成した `activity` 指標を使用した。
- 合成 `activity` の局所ピークの上位候補: 約153.0秒、54.0秒、28.2秒、121.6秒、98.9秒。
- **音響的活動度のピーク**であり、感情、フレーズ終止、クライマックス等を理解した証拠ではない。
- 以前のスクリプトには `numpy` `librosa` `scipy` を使用。依存やバージョンの検証はこれから。

## PoC で作りたい最初の出力

約30〜45秒の音源を自動抽出 → 時系列特徴量・イベント・物語ビート・シーン計画を JSON で生成 → 静止画または暫定プレースホルダーから縦型 MP4 を自動レンダリング。**まず高価な動画生成APIなしでも音楽同期の成否がわかるもの**にする。

## Codex への入口

作業ディレクトリで `prompts/CODEX_START.md` の全文を Codex に貼り付ける。AGENTS.md を読む前提だけにせず、プロンプト側でも参照ファイルを指定済み。

このZIPは「完成コード」ではなく、プロダクト要求・会話背景・検証資産・実装指示を揃えた引き継ぎパッケージ。

## P0 の実行手順（2026-10-19 実測済み）

前提: Python 3.13、FFmpeg 8.1（`ffmpeg` / `ffprobe` が PATH にあること）。
原音源・資料を破壊しない。外部アップロード・生成API・課金APIは使わない。

```bash
uv venv --python 3.13                  # project .venv（初回のみ）
uv pip install -r requirements-p0.txt # 固定版の依存（バージョンは同ファイル参照）
.venv/bin/python -m p0.pipeline       # 分析→選択→plan→両MP4→QA→timeline可視化
echo $?                               # 0 = PIPELINE PASS＋MANIFEST_VERIFY PASS
                                      # （QA全項目＋pytest 38件＋hash再検証）
```

主な成果物: `output/poc_structured.mp4` / `output/poc_naive.mp4`
（540×960・30fps・自動選択 30〜45秒）、`output/analysis.json` /
`output/plan.json` / `output/storyboard.json` / `output/qa.json` /
`output/cost.json` / `output/timeline.png` / `output/timeline.html`
（外部通信なし）/ `output/run_manifest.json` / `output/run.log`。
再現用依存は `requirements-p0.txt`。設計レビューと実測値は
`docs/07_DESIGN_REVIEW_AND_P0.md` を参照。技術方式比較・次段の
受け入れ条件は `docs/09_ARCHITECTURE_DECISION_AND_ACCEPTANCE.md` を参照。
手作業のタイムライン修正は受け付けない（cut 秒の入力口はない）。

## ローカルプレビュー（127.0.0.1専用）

ブラウザで `output/` のMP4をシーク確認するためのローカル配信手順。
標準の `python -m http.server` は Range に未対応（常に200＋全量返却）のため、
シークが `seekable=[0,0]` で0に戻る。代わりに単一範囲対応の小スクリプトを使う。
音声解析・plan・レンダリングには触れない。外部公開・LAN公開はしない。

```bash
.venv/bin/python scripts/serve_preview.py --port 8765
# ブラウザで開く: http://127.0.0.1:8765/poc_structured.mp4
```

仕様: `127.0.0.1` のみbind（変更口なし）、既定でrepoの `output/` を配信、
単一byte range（`start-end` / `start-` / suffix `-N`）に206＋
`Content-Range` / `Content-Length` / `Accept-Ranges: bytes`、HEAD・通常GET・
範囲外416に対応、範囲外のbodyは送らない。標準ライブラリのみ依存なし。
既存 `:8765` プロセスの切替は行わない（親が切替後にブラウザ再検証）。
