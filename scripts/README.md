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

## Releaseの公開用文章（Actions）

`studio-release.yml` は下書き保存、文章生成、公開の3ジョブを直列に実行する。`studio_release.py --stage` は全採用・資産ゲートを検証して下書きを準備する。`--stage` なしの最終呼出しでは保存済みのタイトル・descriptionも必須になる。

`studio_release_copy.py` は `prepare`（GitHub読取）、`generate`（Codex）、`publish`（Releaseのname/body保存）を分離する。`publish` 自体は下書きを公開しない。モデル設定は `.github/studio/config.json` の `publication`、文章規則は[プロンプト](../.github/studio/prompts/codex-publication.md)、出力形式は[スキーマ](../.github/studio/schemas/publication.schema.json)を使う。

```bash
python3 scripts/studio_release.py --help
python3 scripts/studio_release_copy.py --help
.venv/bin/python -m pytest tests/test_studio_publication.py tests/test_candidate_workflow.py -q
```

以下はActionsの実行形。`prepare` / `publish` にだけ `GH_TOKEN` と `GITHUB_REPOSITORY` を渡す。`generate` は既存の `studio-auth` で復号・隔離した `CODEX_RUNTIME_HOME` を使う。

```bash
python3 scripts/studio_release.py --pr "$PR_NUMBER" --stage
python3 scripts/studio_release_copy.py prepare --tag "$RELEASE_TAG" --work "$RUNNER_TEMP/release-copy"
# prepared.json の needed=true のときだけ生成・保存する。
python3 scripts/studio_release_copy.py generate --work "$RUNNER_TEMP/release-copy"
python3 scripts/studio_release_copy.py publish --work "$RUNNER_TEMP/release-copy"
python3 scripts/studio_release.py --pr "$PR_NUMBER"
```

生成結果とログはRUNNER_TEMPに置く。文章生成は録音・動画を取得せず、Release資産を増減しない。途中失敗後も同じ下書き・保存済み文章を再利用する。既存の公開済み仮タイトルは、元の採用PR番号でworkflowをdispatchすると補完できる。

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

`storyboard.json` の任意の `renderer: "storm-recital-v1"` は、登録済みの豪雨のピアノ公演3ショットに雨・照明・濡れた地面の反射を重ねる。`asset_bundle` との同時指定は拒否する。各場面の `params` は `storm_shot`（整数0〜2、既定0）、`rain_density`（0.2〜1、既定0.7）、`rain_wind`（-1〜1、既定0）、`storm_light`（0〜1、既定0.5）、`reflection_strength`（0〜1、既定0.6）を受け付ける。ショット画像と空・地面のマスクは既存DSP境界を50%点として前後最大4フレーム（計8フレーム、30fpsで約0.27秒）でディゾルブする。雨などの数値・パレット・カメラは前後最大24フレーム（計48フレーム）の補間を保ち、各側は隣接場面の4分の1に制限する。短い場面ではショットの補間幅もその制限内に収める。疎な既存DSPアクセントで空のあるショットに枝分かれした雷、俯瞰に反射光を出し、`storm_light` が強度を変える。6色のパレットによる色調・反射・閃光色、`light_bias`、数値カメラも使い、`figure_scale`・`particle_multiplier`・`glow_scale` はこの描画に作用しない。`plan.json` と `provenance.json` の `visual_renderer` はレンダラーID・リポジトリ相対ソースパス・コードのSHA256、`visual_assets` は登録画像とmanifestのハッシュを記録する。描画コードや素材が変われば視覚承認を取り直す。

低水準の処理：

- `studio_download.py --canonical-wav`：v2の可逆復号とWAVメタデータ正規化。音声の実サンプルは変更しない。flagなしは旧契約を保持する。
- `studio_analyze.py`：ネイティブレートの特徴量から60–120秒候補・場面・アクセントを計算する。
- `studio_candidate.py verify-dsp --brief <brief.json> --analysis <plan-context.json>`：手修正していない候補か再解析結果とexact比較する。
- `studio_render.py`：同じ入力から本編・タイムライン・機械QAを生成する。
- `studio_candidate_actions.py`：trusted main用のprepare／seal／review／check／publish。通常はworkflow経由で使用する。
- `studio_visual_preflight.py --codex-led`：静止画と短い無音プレビューの独立レビュー。新方式は `--candidate-context` でPR・元Issue・headのguardを選び、旧方式は子Issueのguardを保つ。

旧 `studio_prepare_project.py` はv1保存済み作品の互換用。`studio_repair.py` とMuse用アダプターは旧実装の回帰検証用に残るが、現行Actionsの制作には使用しない。

## 夜の教会・ろうそくの登録描画

`night-church-v1` と `last-candle-v1` は `studio/nocturne_chapel.py` を使う。
`candle-rift-v1` は、元録音のRMSに追従する炎と、教会の断片が落下して炎・岩・嵐の世界へ進む描画を行う。対応する `project.analysis_profile: "chopin-scherzo3-coda-v1"` が必須で、`init --profile chopin-scherzo3-coda-v1` または `studio_analyze.py --profile chopin-scherzo3-coda-v1` で自動解析する。登録楽譜とのDTW照合と新しい低音の立ち上がりから、長調への到達・両手オクターブ・高速コーダを原音源の整数サンプル位置に求める。先頭2点が場面境界、最後の点が場面内の突入開始となり、時刻を手入力しない。照合に失敗すれば拒否し、従来のDSP場面へ黙って戻さない。全音源をモデルに渡さず、登録楽譜・帰属・ライセンスは `assets/score/chopin-op39-v1/` に保持する。診断の不確かさは窓幅等の支持区間であり、実演に対する精度保証ではない。

同描画の `plan.json` は `semantic_landmarks` と `score_alignment` を保持する。符号化後QAは場面境界に加え、高速コーダの内部cueも実際の描画時刻を前後に動かした反実仮想と比較する。空間誤差・同期の閾値は従来と同じで、専用の合成信号テストで正常動画・1フレーム遅延・欠落アクセントを分けて検証する。固定素材は `assets/visual/candle-rift-v1/` と既存のろうそく画像をハッシュで検証する。

`candle-clearing-v1` は任意の別モードで、`project.analysis_profile: "chopin-scherzo3-coda-clearing-v1"` を使う。4番目の `final_scale_arrival` は最終上行のRH E7到達（第621小節第1拍）であり、音の消失を表さない。元の整数サンプルを含むフレームはcandidateに保持し、実際の晴れ始めはそのサンプル以上となる最初のフレーム境界へ切り上げる。公開planの当該semantic recordは `frame` を発動フレーム、`containing_frame` を元の包含フレームとし、`frame_policy` を記録する。発動後に最低60フレームを確保し、場面は従来と同じ3つのままとする。

この別モードの炎は背景を透過する連続した光として描く。晴れ間は発動フレームから約180フレームで開き、残尺が短い場合は終端までに完了する。元画像の雲の明暗・細部とseedで固定する複数スケールの空間場から、雲ごとの後退順を求める。明るい裂け目から不規則に開き、暗い雲塊は後半まで残す。合成境界は場の勾配に応じた約2画素の幅とし、円弧や広いぼかしの窓を使わない。空だけの登録素材 `assets/visual/candle-clearing-v1/cleared-world.png` を、元画像と同寸法の登録2D空マスク `sky-mask.png` で合成する。マスクは同bundleの `build_mask.py`・`mask-guide.json` から再生成でき、元画像のハッシュと輪郭・前景の凹部を保持する。岩・溶岩を含む画像を空へ複製移動しない。溶岩と奥行きの移動を保ち、稲妻と灰は晴れに従って減らす。元の `candle-rift-v1` の既定描画は変えない。高周波の描画を含むため、符号化後の正常・1フレーム遅延・晴れ欠落の校正は本番の540×960・CRF18・既存patch上限5で行う。180×320での旧校正失敗を本番サイズの合格で置き換えて一般化しない。

事前確認は場面ごとの静止画・動きに加え、通常の場面中点windowが晴れ始めとその後30フレーム以上を示さない場合に最大60フレームのsemantic motion windowを1つ追加する。既存の場面windowを保持し、追加windowと重複させず、終幕の静止画で晴れた状態も確認する。追加windowのIDは `scene03:final_scale_arrival`、schema・合計フレーム上限・contact sheetの再検証も同じ選定を使う。符号化後QAのpatch誤差上限は5のまま、実際の晴れ間を動かす反実仮想で正常・1フレーム遅延・欠落を検査する。

新モードの符号化回帰試験は本番と同じ540×960・CRF18・yuv420pで行う。工程用の小さな確認画像へ合格を一般化しない。旧モードの180×320回帰試験は維持する。事前レビューのpromptでは本人の希望・creative briefを解析診断より先に省略せず渡し、追加windowを含む完全なnative sample対応表も渡す。

登録素材は `assets/visual/nocturne-chapel-v1/` の未変更PNGで、生成記録・利用条件・SHA-256を同梱する。
`chapel_warmth` と `candle_strength` は0〜1の場面パラメータであり、コーダや感情の検出値ではない。
炎は実画像の芯位置に固定し、光・炎・背景に同じカメラ変換を適用する。

Studio解析は、元の多様性フィルターで空きがある場合、録音終端を保持する自動候補も追加する。
従来候補のID・時刻は維持し、終端候補だけは区間の重複を許す。
末尾の不完全な映像1フレーム未満だけを除くため、長い録音後の無音も残り得る。
音楽的な終止やコーダ開始を認識したとは扱わない。
