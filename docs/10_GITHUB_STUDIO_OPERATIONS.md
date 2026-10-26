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
| `Studio release` | PRのMerge、またはownerのmainからの復旧dispatch。採用動画を下書きへ保存し、英語タイトル・description生成後に公開して関連候補を閉じる |
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

`studio-issue-<番号>` を採用枠としてdraft Releaseを作り、14ファイルをアップロード・照合する。次の文章生成ジョブがタイトル・descriptionを保存し、最終ジョブが採用・Artifact・資産・文章を再検証して公開する。公開確認後に同じ元Issueの他候補PRを閉じ、最後に元Issueを閉じる。PR本文の `Fixes` / `Closes` で先にIssueを閉じない。

Releaseは直列実行とし、同じ元Issueで別の候補が先に選択枠を取得していれば上書きしない。二つのPRをMergeしても二作品の採用にはならない。採用は一つだけ。公開済みReleaseの異なる資産の差替えや、自動的な別案への切替は行わない。

## Releaseの公開用文章

`Studio release` は `stage` → `publication` → `release` の順に実行する。最初のジョブがmainを取得してそのSHAを固定し、後続ジョブも同じコードを使う。`stage` は従来の採用・メディア検証をすべて通してから下書きへ資産を保存する。この段階では公開もIssue終了もしない。

`Write publication title and description` が元Issue、本人コメント、採用briefから英語の文章を1組生成する。モデル・推論設定は `.github/studio/config.json` の `publication`（`gpt-6.1-sol / high`）をコマンドへ渡す。タイトルは確認された曲名・作品番号と数単語の映像テーマを `Work / Visual Theme` 形式で組み合わせる。文章規則は[公開用プロンプト](../.github/studio/prompts/codex-publication.md)に集約する。

保存先はReleaseの `name` と `body`。本文の `studio:description:start` / `studio:description:end` 間が公開用descriptionで、末尾に元Issueと既存の採用marker・制作記録を保持する。生成元テキストのハッシュ・モデル・effortもコメントに残す。モデルへ渡すのは文章のみで、動画・録音は渡さない。文章生成ジョブはowner実行に限り、GitHub tokenをモデルから除去する。

生成結果の構造・長さ・日本語等の混入を検査し、書き込み直前に元IssueとReleaseの変更、本人Merge、receipt、タグを再確認する。文章生成・保存に失敗すると下書きのまま止まる。最後の `release` は全メディア検証と保存済み文章の検査を再度通してから公開する。英語の自然さやすべての事実の正しさを機械検査だけで保証するものではない。

保存済み文章は再生成しない。人の編集は上書きせず、管理用の文章マーカーがない編集済み下書きは公開前チェックで停止する。既に公開済みの古い手動文章には新マーカーを要求せず、採用・資産ゲートを通したIssue終了処理の再開を許可する。旧実装による公開済みの仮タイトルも、同じPR番号の復旧dispatchで文章を補完できる。公開済み動画・タグ・資産一覧・旧計画記録は変更しない。

## 失敗と復旧

- **美的・機械的な不合格**：同じCodex制作セッションでPRの指摘を修正する。閾値を広げたり、判定JSONを成功に書き換えたりしない。
- **認証・モデル・実行環境の障害**：作品の判定と分け、原因を修正して同じ入力を再実行する。
- **Releaseのアップロード／公開／クローズ途中の失敗**：採用したPR番号で `gh workflow run studio-release.yml --ref main -f pr_number=104` を実行する。同じdraft／公開済み資産を照合し、未完の処理を復旧する。
- **公開用文章ジョブの失敗**：失敗ジョブを再実行するか、上記dispatchで再開する。新作は文章が保存されるまで下書きのまま。保存済みなら再生成せず、最終ジョブで公開とIssue終了を続行する。再実行でもArtifact失効や採用・ハッシュのゲートを免除しない。
- **Release作成APIの403／404**：過去のマージコミットと現在のmainで `.github/workflows/` が異なる場合、GitHubは作成トークンにもworkflow権限を要求する（[公式API仕様](https://docs.github.com/en/rest/releases/releases#create-a-release)）。既存タグがあっても `target_commitish` を指定する本実装では失敗する場合があり、Actionsの `GITHUB_TOKEN` にはこの権限を付けられない。採用・Artifactの全検証が通った後に限り、本人がworkflow権限を持つ認証で、コントローラーと同じタグ・マージSHA・本文の**下書き**Releaseを準備し、Actionsを再実行する。GitHub CLIの権限追加は本人が `gh auth refresh -h github.com -s workflow` で承認する。公開・資産照合・Issue終了は引き続きActionsが担当し、回避のために対象を最新mainへ変更したり、既存タグを移動したりしない。
- **Artifact失効、head変更、入力不一致**：古いreceiptでは公開しない。再現・独立検証が必要。マージ後の入力を勝手に別内容へ書き換えない。

## 認証

Codexレビューは `CODEX_ENC_APP_ID`、`CODEX_ENC_APP_PRIVATE_KEY`、`CODEX_ENC_PASSPHRASE` を使用する。MuseはActionsに含めない。CLIは `@openai/codex@latest`、モデルは設定とコマンドを `gpt-6.1-sol / xhigh` に揃える。

認証はRUNNER_TEMPへ復号し、GitHub／Artifact tokenをモデルから除去する。read tokenをモデル呼出し前に失効し、変更された認証だけ別のwrite tokenでforceなしに保存する。失敗時にも認証保存と削除を行う。モデルを使うworkflowとReleaseの文章生成ジョブは `studio-agent-auth` で直列化する。Release全体は別の直列キューで採用枠を保護する。

## 既存v1作品

`studio/projects/issue-1/` と既存の制作PR #6は旧入力・receipt・計画記録を保持する。`Studio production` と従来のRelease controllerが対応し、**採用承認は新作と同じく本人のMergeのみ**とする。Approve不足で停止したマージ済みPRは、本人がmainから `studio-release.yml` をそのPR番号で再実行できる。動画は再生成せず、既存の合格Artifact、head／merge内入力、ハッシュ、機械QA・美的レビューを再照合する。新作の4 JSON契約に見せかけて古いArtifactを流用しない。旧作品の子Issue終了規則も従来契約を維持する。
