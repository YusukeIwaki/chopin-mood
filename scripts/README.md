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

新作は元Issueへ直接紐づく最大3候補。手順は [studio-produce](../.agents/skills/studio-produce/SKILL.md)、裁定は [レビュー規約](../docs/12_CREATIVE_REVIEW_PROTOCOL.md)。以下の番号は実際のIssue・PRに置き換える。`gh` は本人アカウントで認証する。完全な音源は無視対象の `output/` にだけ置く。

```bash
# ownerのIssueを取得し、未切り取り録音からDSP候補を作る。再実行で既存入力は上書きしない。
.venv/bin/python scripts/studio_candidate.py init --issue 103 --max-candidates 3 --creator-id production-session

# Codexが企画・storyboardを制作し、静止画と短い動きを確認した後、本編を生成する。
.venv/bin/python scripts/studio_candidate.py render --issue 103 --candidate cand-01 --wav output/studio/issue-103/source.wav --creator-id production-session --round 0

# 出力された editorial.template.json に独立2役の評価と編集判断を記入し、候補のeditorial.jsonへ保存する。
# テンプレートは未レビュー。passにするために証拠や不合格を消さない。
.venv/bin/python scripts/studio_candidate.py validate --issue 103 --candidate cand-01 --media-dir output/studio/issue-103/cand-01/round-0

# PR作成後、Codexが再現・独立レビューを依頼する。
gh workflow run studio-candidate.yml --ref main -f pr_number=104

# 明示された改訂依頼で本人コメントを取得する（書込み・自動制作はしない）。
.venv/bin/python scripts/studio_candidate.py feedback --pr 104 --out output/studio/issue-103/feedback-pr-104-request-1.json
```

候補を `codex/studio-issue-103-cand-01` のような別ブランチにコミットし、1案1PRにする。4入力の保存先は `studio/projects/issue-103/candidates/cand-01/`。他候補のファイルを同じPRへ混ぜない。PR作成は通常のgit／gh操作を使い、作成したPRをCodexのtaskへattachする。改訂も同じブランチで行う。PRを提示するときは対象headのActions成功とArtifactを確認する。

`render --round 1` / `--round 2` で修正証拠を別保存する。同じ出力先に動画があれば上書きを拒否する。新しい本人依頼でroundを0へ戻す場合は新しい `--out output/...` を使い、PRに依頼と前版を記録する。`validate --offline` は診断用であり、ownerのライブ確認を置き換えない。

`init` と `render` はエンジン・素材・依存ファイルが `project.engine_sha` と同じことを確認する。新しい描画コード・素材は先にエンジニアリングPRへまとめる。Actionsはdispatch時のmain SHAと照合し、PRコードを実行しない。

`storyboard.json` の任意の `renderer: "ribbon-dialogue-v1"` は、画像素材を使わないオリジナルの紙リボン描画を選ぶ。`asset_bundle` との同時指定は拒否する。各場面の `params` に `dialogue_spread`・`dialogue_twist`（-1〜1）と `dialogue_lift`（0〜1）を指定でき、既存のDSP境界で滑らかに補間する。既定値は順に0・0・0.35。パレット、`light_bias`、数値カメラも使い、`figure_scale`・`particle_multiplier`・`glow_scale` は紙リボンに作用しない。`plan.json` と `provenance.json` の `visual_renderer` はレンダラーID・リポジトリ相対ソースパス・コードのSHA256を記録し、描画コードが変われば視覚承認を取り直す。

低水準の処理：

- `studio_download.py --canonical-wav`：v2の可逆復号とWAVメタデータ正規化。音声の実サンプルは変更しない。flagなしは旧契約を保持する。
- `studio_analyze.py`：ネイティブレートの特徴量から60–120秒候補・場面・アクセントを計算する。
- `studio_candidate.py verify-dsp --brief <brief.json> --analysis <plan-context.json>`：手修正していない候補か再解析結果とexact比較する。
- `studio_render.py`：同じ入力から本編・タイムライン・機械QAを生成する。
- `studio_candidate_actions.py`：trusted main用のprepare／seal／review／check／publish。通常はworkflow経由で使用する。
- `studio_visual_preflight.py --codex-led`：静止画と短い無音プレビューの独立レビュー。新方式は `--candidate-context` でPR・元Issue・headのguardを選び、旧方式は子Issueのguardを保つ。

旧 `studio_prepare_project.py` はv1保存済み作品の互換用。`studio_repair.py` とMuse用アダプターは旧実装の回帰検証用に残るが、現行Actionsの制作には使用しない。
