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

## Studio 修復エントリポイント（開発者向け）

`scripts/studio_repair.py` は初回 Codex レビューと manifest 生成の後、最終アップロード／公開の前にワークフローから呼ばれる有界修復の入口である。運用手順ではなく開発者向けの契約だけを記す。

```bash
python3 scripts/studio_repair.py --work output/studio/work \
  --muse-work "$RUNNER_TEMP/muse-work" \
  --wav output/studio/source/source.wav \
  --parent <親Issue> --child <子Issue> --engine-sha <github.sha> \
  --run-id <run_id> --run-attempt <run_attempt>
```

- 入力: `work` 配下の初回12ファイル bundle（hash／receipt／機械 QA／head 結び付きレビューを検証）、`brief.json`／`candidate.json`／`scenes.json`／`pr.json`、復号済み `source.wav`。環境変数 `GH_TOKEN`／`GITHUB_REPOSITORY` が必要。GitHub トークンは隔離ワーカーへ渡さない。
- 動作: 初回を `work/review-attempts/attempt-01` に保存する。合格または engine 経路ではモデルを呼ばない。`storyboard` 経路では最大2回まで、隔離 Muse → trusted render → commit → receipt → Codex 再レビューを行う。各エージェントは最大900秒。元のタイミングと音源ハッシュは変更しない。
- 出力: 最終レビュー済み bundle が `work/bundle` に残り、`work/pr.json`／`work/storyboard.json` が最終試行と一致する。運用結果は bundle の外の `work/repair-result.json` に書き、`GITHUB_OUTPUT` に `head_sha`／`pr_number`／`status` を出す。status は `passed`／`needs_engine`／`attempts_exhausted`／`no_change`／`no_visual_change` のいずれか。
- fail-closed: malformed／stale／closed／source drift／機械 QA 失敗は非ゼロ終了し、公開しない。生の provider ログ・プロンプト・秘密情報・原音源は試行スナップショットへ入れない。
