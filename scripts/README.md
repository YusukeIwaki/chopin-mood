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
