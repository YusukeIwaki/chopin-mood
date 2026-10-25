# Studio運用

人向けの流れは[README](../README.md)、制作担当の手順は[studio-produce](../.agents/skills/studio-produce/SKILL.md)、評価と裁定は[創作レビュー規約](12_CREATIVE_REVIEW_PROTOCOL.md)を参照する。

## 制作と候補PR

新作は元Issueへ直接結び付ける。子Issueを作らず、最大3候補をそれぞれ別のPRにする。Issueの作成・コメントだけではモデルを起動せず、Codexへの制作／改訂依頼を入口にする。

保存先は `studio/projects/issue-<番号>/candidates/cand-01/` 〜 `cand-03/`。

| 入力 | 内容 |
| --- | --- |
| `project.json` | v2、元Issue、候補ID、添付URL、検証したengine SHA |
| `brief.json` | 本人の希望、創作案、原音源ハッシュ、DSP候補と整数時刻、fingerprint |
| `storyboard.json` | レンダラーが実行する構図・パレット・カメラ・登録固定素材 |
| `editorial.json` | ローカルの動画／入力ハッシュ、機械QA、音楽・映像の独立レビュー、編集責任者の裁定 |

ブランチは `codex/studio-issue-<番号>-cand-01` の形式とする。候補PRの差分はその候補の4 JSONだけ。元音源、動画、認証、モデルの非公開ログをコミットしない。新しい固定素材・描画機能はエンジニアリングPRで先にレビューする。候補を公開する前にエンジンの変更が必要なら、制作担当がその作業も引き受ける。

ローカルで `studio_candidate.py init` から取得・解析し、描画を改善する。静止画、短い動き、本編を確認し、独立レビューと裁定を記録して `validate` を通す。具体的な呼出しは[開発ガイド](../scripts/README.md)に置く。helperは制作を補助するもので、自動的に美的合格を付けない。

候補ごとのコミット・PR作成後、制作担当が以下を実行する。人がActions画面で毎回作業する必要はない。

```bash
gh workflow run studio-candidate.yml --ref main -f pr_number=104
```

実際のPR番号に置き換える。実行アカウントと元Issue作成者は `YusukeIwaki` に限る。PRは同一リポジトリのownerまたは許可bot作成で、baseはmainであることをAPIで再確認する。forkの実行コードを認証付きで動かさない。

## Actionsの役割

| Workflow | 起動と責務 |
| --- | --- |
| `Studio candidate` | ownerがmainからPR番号をdispatch。ローカル検証済みの保存入力を再現し、独立レビューとArtifact保存を行う |
| `Studio release` | PRのMerge、またはownerのmainからの復旧dispatch。採用動画を照合して公開し、関連候補を閉じる |
| `Studio CI` | push／PR。Secret・モデル・本人録音なしのテストと合成信号レンダリング |
| `Studio auth smoke` | ownerによるCodexの最小認証確認 |
| `Studio production` | 旧v1保存プロジェクト専用。既存作品の互換経路として維持 |

新方式の再現処理は次の順で進む。

1. 信頼済みmainをcheckoutする。PRのheadをAPIで固定し、4 JSONだけを取得する。ローカル編集記録の機械QA・必須レビュー・入力ハッシュ・修正上限を検査する。
2. 音源を一時領域へ取得し、添付と復号WAVのハッシュを照合する。v2はPCMバイトを変えずにWAVの可変メタデータを除く。実デコーダ差による音声サンプル不一致は止める。
3. DSPを再計算し、候補が同じ出力かexact比較する。手入力した時刻は採用しない。
4. 各場面の静止画、短い無音プレビューを生成し、それぞれ独立したCodex呼出しでレビューする。途中結果は同じ候補PRへ記録する。レビュー・アップロード・コメント前にも元IssueとPRのhead／入力を確認する。
5. 合格した同一入力から本編を生成し、圧縮後の音声・映像の機械QAと最終Codex映像レビューを実施する。
6. 許可リストとハッシュを検査したArtifactを保存する。機械・美的ゲートの両方が合格した場合だけ、本人へassignし `@YusukeIwaki please check this` とCOMMENTする。AIによるApproveではない。

原音源をモデルへ渡さず、Actionsはコンテンツを新たに制作・修復しない。不合格は非成功で終了し、PRの証拠を制作担当が引き取る。静止画／短いプレビューのレビューを、全編を聴いて視聴した判断と表現しない。

`project.engine_sha` とdispatch時のmain SHAは一致が必要。mainが先に更新された場合は新しいエンジンでローカル再検証して入力と証拠を更新する。古い合格を流用する自動リベースはしない。これは保守的な再現条件であり、コードの無関係な更新を除外する最適化は未実装。

## 改訂

「PR #104 のコメントに対応」の依頼で、最新headと本人コメントを取得する。コメントの取得記録はgit管理外へ保存し、PR本文に希望ごとの対応・証拠・未解決事項を残す。同じ候補・PRを更新してローカル検証、Actions再現をやり直す。

1依頼につき初回＋最大2回の実質修正を静止画から本編まで共有する。新しい本人の依頼で再開するときも、以前の不合格や裁定を履歴から消さない。`editorial.round` は現在の依頼の範囲を検査するが、全セッションを通じた回数を暗号学的に証明するものではない。Git履歴とPRの依頼・回答も確認する。

## Artifactと採用

本編Artifact名は `studio-candidate-pr-<PR>-<head>-run-<run>-a<attempt>`。保存期間は90日。14ファイルだけを許可する。

- 作品と確認用：`video.mp4`、`contact-sheet.jpg`、`timeline.html`
- 4入力：`project.json`、`brief.json`、`storyboard.json`、`editorial.json`
- 再現結果：`plan.json`、`timeline.json`、`qa.json`、`provenance.json`
- 承認の結合：`review.json`、`receipt.json`、`hashes.json`

ローカル動画とActions動画はエンコーダ環境でバイトが異なり得る。PRへ提示して採用するのはActionsが検証したArtifactの実バイトであり、ローカル動画と同じバイトだとは主張しない。ローカル証拠とActions証拠は同じ制作入力・エンジンへ結び付け、別々のQAとレビューを持つ。

本人がMergeしたPRを採用する。既存v1作品を含め、別途Approveは要求しないが、Merge済みというだけで検証を免除しない。Releaseは次を再確認する。

- 本人のMerge、同じリポジトリ／main、レビュー対象headとmerge結果の4入力の一致。
- ownerの信頼済みmain workflow実行、そのrun／attemptに属する未失効Artifact、botの該当headへのreceipt。
- 全ファイルのハッシュ、原音源・メディアのprovenance、機械QA、ローカル裁定、Actionsの美的合格。

`studio-issue-<番号>` を採用枠としてdraft Releaseを作り、14ファイルをアップロード・照合して公開する。公開確認後に同じ元Issueの他候補PRを閉じ、最後に元Issueを閉じる。PR本文の `Fixes` / `Closes` で先にIssueを閉じない。

Releaseは直列実行とし、同じ元Issueで別の候補が先に選択枠を取得していれば上書きしない。二つのPRをMergeしても二作品の採用にはならない。採用は一つだけ。公開済みReleaseの異なる資産の差替えや、自動的な別案への切替は行わない。

## 失敗と復旧

- **美的・機械的な不合格**：同じCodex制作セッションでPRの指摘を修正する。閾値を広げたり、判定JSONを成功に書き換えたりしない。
- **認証・モデル・実行環境の障害**：作品の判定と分け、原因を修正して同じ入力を再実行する。
- **Releaseのアップロード／公開／クローズ途中の失敗**：採用したPR番号で `gh workflow run studio-release.yml --ref main -f pr_number=104` を実行する。同じdraft／公開済み資産を照合し、未完の処理を復旧する。
- **Artifact失効、head変更、入力不一致**：古いreceiptでは公開しない。再現・独立検証が必要。マージ後の入力を勝手に別内容へ書き換えない。

## 認証

Codexレビューは `CODEX_ENC_APP_ID`、`CODEX_ENC_APP_PRIVATE_KEY`、`CODEX_ENC_PASSPHRASE` を使用する。MuseはActionsに含めない。CLIは `@openai/codex@latest`、モデルは設定とコマンドを `gpt-6.1-sol / xhigh` に揃える。

認証はRUNNER_TEMPへ復号し、GitHub／Artifact tokenをモデルから除去する。read tokenをモデル呼出し前に失効し、変更された認証だけ別のwrite tokenでforceなしに保存する。失敗時にも認証保存と削除を行う。モデルを使うworkflowは `studio-agent-auth` で直列化し、Releaseは認証を使わない別の直列キューで動かす。

## 既存v1作品

`studio/projects/issue-1/` と既存の制作PR #6は旧入力・receipt・計画記録を保持する。`Studio production` と従来のRelease controllerが対応し、**採用承認は新作と同じく本人のMergeのみ**とする。Approve不足で停止したマージ済みPRは、本人がmainから `studio-release.yml` をそのPR番号で再実行できる。動画は再生成せず、既存の合格Artifact、head／merge内入力、ハッシュ、機械QA・美的レビューを再照合する。新作の4 JSON契約に見せかけて古いArtifactを流用しない。旧作品の子Issue終了規則も従来契約を維持する。
