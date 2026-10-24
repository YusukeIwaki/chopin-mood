# ローカル開発ガイド

すべてのコマンドはリポジトリ直下で実行する。パスはリポジトリ相対で指定する。

## 前提

- Python 3.13
- `ffmpeg` と `ffprobe` が PATH にあること
- `uv`

```bash
uv venv --python 3.13
uv pip install -r requirements-p0.txt
```

## 合成 smoke とテスト（個人の録音・Secret 不要）

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/python scripts/studio_smoke.py --out output/studio-smoke
```

`studio_smoke.py` は合成信号から候補・storyboard・描画・符号化 QA までを通す。
個人の録音や Secret は使わない。

## P0 オフライン比較（30〜45秒）

P0 は従来のオフライン比較用である。現在の Studio 制作（60〜120秒）とは尺が異なる。

所有する WAV を明示的に指定して実行する。例として `assets/my-performance.wav` を使うが、このファイルは利用者自身が用意するもので、クローンには含まれない。

```bash
.venv/bin/python -m p0.pipeline --audio assets/my-performance.wav --out output
```

出力は `output/poc_structured.mp4` と `output/poc_naive.mp4`（縦型 MP4）、
`output/analysis.json`、`output/plan.json`、`output/qa.json`、
`output/timeline.png`、`output/timeline.html` などである。
カット秒を指定する入力口はない。

`assets/reference_audio.wav` がある場合、それは解析専用の 22.05kHz モノラル約167秒であり、公開用のマスターではない。私的な素材はクローン直後には存在しないため、既定パスのまま実行できるとは限らない。

## プレビュー

生成後に `output/` をローカルで確認する。

```bash
.venv/bin/python scripts/serve_preview.py --port 8765 --directory output
```

ブラウザで `http://127.0.0.1:8765/` を開き、MP4 のシークを確認する。
このサーバーはローカル確認専用である。

## 校正（追跡対象スクリプト）

```bash
.venv/bin/python scripts/calibrate_sample_rate.py
.venv/bin/python scripts/calibrate_structure_midpoint.py
.venv/bin/python -m pytest tests/test_sample_rate_features.py tests/test_structure_midpoint.py -q
```

校正は合成信号による音響的校正であり、音楽的・美的合格を意味しない。詳細は [レート校正](../docs/11_SAMPLE_RATE_TUNING.md) を参照する。

## 旧解析スクリプトについて

`scripts/analyze_reference.py` は旧解析の再実行と比較のための参照スクリプトであり、現在の production 解析器ではない。Studio の候補生成は `scripts/studio_analyze.py` と `studio/` 配下が担う。

## Codex主導のStudio制作

制作入力は `studio/projects/issue-<親Issue番号>/` の `project.json`、`brief.json`、`storyboard.json` に保存する。原録音は含めない。

- `scripts/studio_prepare_project.py` は保存済み入力とGitHub上の作成者・音源・創作方針・既存Issueの関連を検証し、Actions用の入力を出力する。モデル呼び出しや子Issueの作成は行わない。
- `scripts/studio_visual_preflight.py --codex-led` は、静止画と短い無音プレビューを順に生成・レビューする。新しい子Issueや修復ワーカーは起動しない。Actionsのrun・engine・artifactに結び付けるため、本番の呼び出し引数はワークフローを参照する。
- `scripts/studio_render.py` は同じ入力から本編・タイムライン・機械QAを生成する。本番では途中承認のハッシュとengine SHAも照合する。
- 不合格時は制作担当のCodexが同じ作品の絵コンテや描画コードを修正する。変更前後で原音源・自動計算した時刻・機械QA基準を保持する。

以前の `scripts/studio_repair.py` とMuse用アダプターは旧実装の回帰検証用に残っているが、現在のActionsでは使用しない。
制作・確認・公開の流れは [README](../README.md)、認証と成果物の契約は [Studio運用](../docs/10_GITHUB_STUDIO_OPERATIONS.md) を参照する。
