# Studio運用

制作はCodexセッションで行い、Actionsは保存された作品の再現と検証を行う。
人向けの制作フローは[README](../README.md)、エージェントのルールは[AGENTS.md](../AGENTS.md)を参照する。

## 保存する制作入力

`studio/projects/issue-<親Issue番号>/` に3ファイルを置く。

| ファイル | 内容 |
| --- | --- |
| `project.json` | schema version、親Issue、既存制作Issue、GitHub添付URL |
| `brief.json` | 元の候補、整数サンプル／フレーム位置、企画、音源ハッシュ、fingerprint |
| `storyboard.json` | Codexが制作した場面別パレットと実行可能な数値設定 |

コードと一緒にレビューしてmainへ保存する。個人の録音をgitへ入れない。
現時点では既存の制作Issue・PRを引き継ぐ入力契約であり、未登録の作品を自動分割する機能は使わない。
`studio_prepare_project.py` は、ファイル、元Issueの作成者／Open状態／添付／創作方針、既存制作Issueのbot markerと親子関係を検証する。作成・更新APIは呼ばない。

## Actions

- `Studio production`: オーナーによるmainからの `workflow_dispatch` のみ。`issue_number` に親Issueを指定する。
- `Studio CI`: Secret・モデル・録音なしで、テストと合成音源の書き出しを行う。
- `Studio auth smoke`: Codexだけの最小認証確認。Museのインストール・実行・復号・認証保存はない。
- `Studio release`: 本人が承認・マージした制作PRの成果物を照合して公開する。

productionは次の順に進む。

1. APIで元Issueの作成者と実行者がともに `YusukeIwaki`、IssueがOpen、添付が1件であることを確認する。
2. 保存済み制作入力を読み込み、原録音を一時領域へダウンロード・可逆復号する。添付と復号WAVの両ハッシュを検証する。
3. 各場面の開始／中間／終了の静止画を生成し、Artifactへ保存して既存制作Issueへ開始コメントを出す。Codexが実画像をレビューし、検証済みJSONから結果コメントを出す。
4. 静止画合格後、各場面の中点付近から最大2秒の無音プレビューを生成する。デコードしたサンプル画像を別のCodex呼び出しでレビューする。
5. 両方に合格した同一入力だけ本編をレンダリングし、既存の機械QAを実行する。新しい制作コミットとPRを作成または更新する。
6. exact-headのreceiptを作り、本編の画像・ハッシュ・QAをCodexがレビューする。12ファイルの許可リストに一致するbundleだけアップロードする。
7. 最終レビューと機械QAの両方が合格した場合だけ、本人をPRへassignし `@YusukeIwaki please check this` とCOMMENTする。

途中コメントへの返信や人間の承認は不要。同じrun内の検証済みJSONが進行を制御し、`issue_comment` トリガーは使わない。
不合格ならその段階で停止する。Actions内でコンテンツを修復するモデル呼び出しや、新しいエンジニアリングIssue作成は行わない。制作担当のCodexが同じIssueを継続する。

## 品質状態

ワークフローの成功は作品の承認を意味しない。
`needs_engine` / `visual_blocked` は静止画または動きの不合格であり、本編は生成されない。
最終 `review.json` のfailも公開対象外である。具体的な指摘は制作IssueまたはPRに残る。
インフラ障害や不正なJSON・入力の差し替えは非ゼロ終了する。

各モデル呼び出しは15分、途中レビューは40分、build全体は120分を上限とする。
途中のreview履歴は保存し、不正なモデル出力・生ログ・認証・原録音全体はArtifactへ入れない。

## 成果物と承認

- 途中成果物: `studio-visual-child-<id>-run-<run>-a<attempt>-v1-still|motion`。
- 途中履歴: `studio-visual-reviews-child-<id>-run-<run>-a<attempt>`。
- 本編: `studio-child-<id>-pr-<pr>-<head>-run-<run>-a<attempt>`。
- 保持期間は90日。期限切れ成果物に旧承認を流用しない。

途中承認は、source・brief・candidate・storyboard・plan・controlsのハッシュ、寸法、seed、engine SHA、run、attemptに結び付く。本編は書き出す直前に同じ入力か再検証する。
原音源の相対パスは内部処理だけに使い、公開JSONへ絶対パスを含めない。

制作コミットに保存するのはbrief、storyboard、plan、QA、provenance、timelineである。
commit後に生成する `receipt.json` は、PR/head/run/attempt/engine/音源/企画/commitファイルを結び付ける。
provenanceはMP4・確認画像・timeline.htmlの実バイトをハッシュで結び付ける。
これらを更新せず、画像・動画・承認だけを差し替えてはならない。

Releaseには、本人による現在のheadへのApproveと本人によるMerge、信頼できるproduction runの成功、receiptと全成果物の一致が必要。
botはApprove／Mergeしない。COMMENTを本人のApproveと混同しない。
Release公開後に制作Issueを閉じ、全候補が公開済みまたは本人により明示却下された場合だけ親Issueを閉じる。
PR本文に `Fixes #child` を書かず、Release前に自動クローズさせない。
既存の公開済みReleaseに異なる資産を上書きしない。

## 認証・実行環境

既存Codexレビュー経路のSecretは `CODEX_ENC_APP_ID`、`CODEX_ENC_APP_PRIVATE_KEY`、`CODEX_ENC_PASSPHRASE` の3件。
Muse用Secretはproduction／auth smokeから参照しない。保存済みSecret自体を自動削除する必要はない。

Codex CLIはNode 24で `@openai/codex@latest` をインストールし、バージョンをログへ記録する。
read tokenでcodex.encをcloneした後、モデル呼び出し前にtokenを失効する。
復号先はRUNNER_TEMP配下。モデルにはGitHub／Artifact tokenを渡さない。
認証が変わったときだけ再暗号化し、新しいwrite tokenでforceなしに保存する。remoteが動いていれば上書きせず失敗する。
認証の保存と削除は不合格時も実行する。共有認証の競合を防ぐため `studio-agent-auth` の並列実行を禁止する。

公開リポジトリでのChatGPT認証のCI持ち込みは、[現在の公式ガイド](https://learn.chatgpt.com/docs/auth/ci-cd-auth)の推奨条件を満たさない。既存経路をAPI認証へ移行する際は、モデルの利用可否と費用上限を確認してから変更する。

## 制限

DSP境界は音楽的フレーズの正解ではない。音声聴取による自然さの評価は別に行う。
静止画・動きのサンプル画像によるレビューで、全編の連続した動きや演奏との一体感まで確認したと表現しない。
曲名や時期は未指定ならunknownとし、添付ファイル名から断定しない。
現在の作品はCodex内蔵画像生成で作った背景・透過人物素材を使用する。`asset_bundle` は既知の登録IDのみ受け付け、パスやURLを受け付けない。素材のハッシュはmanifest、公的planの承認用ハッシュ、provenanceに含まれ、差し替えには再レビューが必要となる。生成方法とプロンプトは素材ディレクトリに保存する。手続き描画は合成信号の検証用として残す。

Actionsは固定素材を読み込み、カメラ・色・光・粒子だけを決定的に動かす。画像生成や生成動画APIを呼ばず、同期時刻もモデルに決めさせない。追加APIへの課金・外部への演奏送信・投稿は事前承認なしに実行しない。
