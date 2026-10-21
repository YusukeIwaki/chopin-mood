# 10 — GitHub Studio 運用（管理者／運用者向け）

本書は管理者・運用者向けの手順である。利用者向けの制作フロー図と概要は [README.md](../README.md) を参照する。
初版の描画は手続きプロトタイプであり、完成アニメの品質は保証していない。

## セットアップ（管理者）

1. リポジトリ Secrets に正確に4件を登録する:
   `CODEX_ENC_APP_ID`、`CODEX_ENC_APP_PRIVATE_KEY`、`CODEX_ENC_PASSPHRASE`、`MUSE_ENC_PASSPHRASE`。
   GitHub App は `YusukeIwaki/codex.enc` と `YusukeIwaki/muse.enc` の clone／更新専用であり、プロジェクト変更は `GITHUB_TOKEN` で行う。
2. Settings → Actions → General で **「Allow GitHub Actions to create and approve pull requests」** を有効化する。
   本スタジオは COMMENT レビューしか出さないが、PR 作成に必要である。
3. 既定ブランチは `main` とする。production は `main` のイベント SHA を固定して checkout し、手動実行も `main` 限定とする。release は信頼できる `main` を checkout する。いずれも `persist-credentials: false` とする。
4. Codex のバージョンは固定しない。Node 24 で `npm install --global "@openai/codex@latest"` により最新 CLI を入れ、`codex --version` の出力を run ログに記録する。
   Ubuntu には `bubblewrap` と専用 AppArmor プロファイルを公式手順で導入し、認証復号前に名前空間の作成を検証する。読み取り専用・ネットワーク禁止の Codex 権限プロファイルは維持する。
   Muse は公式 launcher（muse.enc の Dockerfile と同方式）で Docker イメージ内に導入し、実バージョンを run ログに記録する。
   モデル指定の正本は `.github/studio/config.json` であり、本書に重複して固定しない。
5. Actions → Studio auth smoke を手動実行し、両モデルの実認証を確認してから録音 Issue を作る。`invalid_refresh_token` なら Codex を再ログインして codex.enc を更新する。

### 認証の保管場所

- Codex の認証は `YusukeIwaki/codex.enc` から `CODEX_ENC_PASSPHRASE` で復号する。
- Muse の認証は `YusukeIwaki/muse.enc` から `MUSE_ENC_PASSPHRASE` で復号する。Muse の認証を codex.enc から復号しない。
- Linux の Muse 認証は `$XDG_CONFIG_HOME/muse/auth.json` へ復号する。macOS の keychain ポインタは移植できない。
- 復号先は `RUNNER_TEMP` 配下のみとする。

### 認証ライフサイクル

- read token で `.enc` リポジトリを clone し、**エージェント実行前に read token を失効**させる。
- 復号時に内容を表示せずに検証する。Codex は `auth_mode == "chatgpt"` かつ `tokens.refresh_token` があること、Muse は keychain ではない schema-v1 の実トークン JSON であることを確認する。
- エージェント後に hash が変わった場合のみ AES-256-CBC／PBKDF2／base64 で再暗号化し、write token を新規に作って **force なし**で push する。clone 時点から remote が動いていたら credentials の rebase はせず失敗させる。Muse の認証保存ブロックは修復ステップの後（最終モデル呼び出しの後、cleanup の前）にあり、修復時の認証変化も成功・失敗を問わず保存する。
- codex.enc／muse.enc への同時書き込みは別リポジトリのワークフローとも競合しうる。失敗安全側に倒れる（上書きしない）ため、その場合はワークフローを再実行する。

## 受付とオーナーガード

- `studio-production.yml` の preflight は、author と sender がともに `YusukeIwaki`、Issue が open、添付がちょうど1件であることを確認する。満たさなければ job summary に理由を書いて正常終了する（graceful skip）。
- 手動 dispatch でも API 側で作成者を再検証する。dispatch 実行者がオーナーであることだけを根拠にしない。
- `studio-auth-smoke.yml` はオーナーの手動実行専用であり、実モデルへの最小プロンプトだけを行う。Issue／PR／Release は変更しない。
- CI（`studio-ci.yml`）はイベント側リビジョンを checkout し、Secret・モデル・個人音源なしでテストと合成 smoke を実行する。

## 制作から公開までのゲート

全体の流れは README のフロー図を参照する。ここではゲートの不変条件だけを記す。

- repo に commit されるのは安定した制作ソース（`brief.json`、`storyboard.json`、`plan.json`、`qa.json`、`provenance.json`、`timeline.json`）のみであり、そのハッシュに最終 Git commit SHA は含めない。
- commit 後に作る artifact 専用 `receipt.json` が、PR／child／parent／head SHA／run ID／run attempt／engine SHA／原音源ハッシュ（添付そのもの＋復号 WAV）／企画 fingerprint／全 commit ファイルの sha256 を結び付ける。自己完結の `timeline.html` も artifact と Release に含める。
- commit する `provenance.json` には MP4・確認画像・`timeline.html` そのものの SHA256 も記録する。同じ設計 JSON から作られた別の動画へ差し替え、artifact のハッシュ表だけを更新しても公開できない。
- Codex レビューは `reviewed_head_sha` を返し、ゲートは期待 head と完全一致、かつ4チェック（機械 QA・brief 準拠・タイミング完全性・美的承認）が全て true、blocker なしを要求する。
- 初回レビュー後に `scripts/studio_repair.py` が有界修復を行う。最終成果物名と publish-review の PR／head は初回 commit ではなく最後の `repair` ステップ出力を使う。初回 receipt／レビューの入力は初回 commit を指したままにする。publish-review には `output/studio/work/repair-result.json` を渡す。
- レビュー済みの各試行は `output/studio/work/review-attempts/attempt-*`（最大3件、各12ファイルの許可リスト）に保存し、アップロード前に `studio.repair.validate_attempt_history` で連番・許可リスト・ハッシュ／receipt／メディア provenance を厳格に検証する（ルート直下の余分なファイルや symlink は拒否する）。履歴成果物名は `studio-review-attempts-child-<child>-run-<run>-a<attempt>` で、Release 対象の成果物名パターンには一致しない。保持期間は90日。未検証の途中 staging はアップロードしない。
- 最終ステップ概要には品質 status、試行回数、次の一手を示す。美的失敗でもワークフロー実行自体は完了しうるが、オーナー確認可能（owner-ready）とは扱わない。
- 通知（receipt コメント、exact-head の COMMENT ping、担当者割当て）はそれぞれ独立に冪等化し、通知直前に head／open を再取得する。ユーザーが書いた marker 文字列は bot 証跡として数えない。
- exact-head の本人 Approve が必要である。COMMENT は承認を消さず、CHANGES_REQUESTED・DISMISSED は承認として扱わない。制作 PR をエージェントが自動で Approve／Merge しない。
- Release の前提は次のすべてである: PR がオーナー本人により Merge 済みであること、最新の実質的なオーナーレビューが現在の head に対する APPROVED であること（COMMENT は承認を取り消さない）、`main` 上で信頼できる studio-production run が実際に成功し、成果物が一致すること。
- Release は 404 のときだけ新規作成し、draft は `target_commitish` を merge SHA として作る。既存資産は全て digest 照合し、draft の不足分だけを足して、正確な最終集合を確認してから公開する。公開済み Release は欠落があっても変更しない。食い違いは上書きせず失敗する。
- Release は draft を ID で再開し、公開後のタグが本人の merge commit を指すことまで確認する。
- 制作 matrix では1候補の失敗で他候補を中断しない。美的な却下はレビュー結果として完了し、通知を出さない。インフラ障害で production run 全体が失敗した場合は公開を保留する。
- artifact 保持期限が切れた場合は、黙って再生成して旧承認を使わず、新しい成果物を再レビューする。

### 子 Issue と親 Issue の閉じ方

- 子 Issue は Release 成功後にだけ閉じる。PR 本文に `Fixes #child` は書かない（Release 成功前に子が閉じてしまうため）。
- 親 Issue は、全ての子が公開済み Release として検証済み、または本人が `not_planned` で明示却下した場合にだけ閉じる。却下の判定では `closed_by.login` がオーナーであることを確認する。

## 再実行・手動操作

- 失敗した制作の再試行: Actions → Studio production → Run workflow に source Issue 番号を入れる（実行者もオーナーのみ、`main` から実行）。保存済みの企画と候補を再利用し、終了済みの子 Issue／PR は制作対象から除外する。手動再実行はコード修正後や利用上限（quota）の解消後に使うもので、通常の絵コンテ反復に手動再実行は不要である（絵コンテ反復はワークフロー内の自動修復が担う）。
- 創作的に却下された場合: 経路が `storyboard` なら、ブロッカーと修復指示の全文と前回の絵コンテを保持したまま Muse が最大2回まで自動修復し、各回で再描画・機械 QA・Codex 再レビューを行う。元のタイミング（抜粋区間・場面境界・アクセント）は変更しない。秒指定・タイミング修正は不要である。
- Release の再試行: Actions → Studio release → Run workflow に PR 番号を入れる。既存 Release の資産が食い違う場合は黙って上書きせず失敗する。
- ソースや brief を変えたい場合: 既存の承認済み作業は上書きしない。**新しい source Issue を開く**こと（初版の制約）。

## 修復 status とエンジニアリング引き継ぎ

最終 bundle と同じ head に結び付いた `repair-result.json` の `status` が品質状態である。ワークフローの成否とは別に読む。

| status | 意味 | 次の一手 |
| --- | --- | --- |
| `passed` | 最終レビュー合格（機械 QA・4チェック・blocker なし） | exact-head のオーナー Approve と Merge を待つ |
| `needs_engine` | レビューが engine 経路（不足した描画・構成・検証能力など） | エンジニアリング Issue からコード変更する |
| `attempts_exhausted` | 絵コンテ修復を上限2回まで使っても不合格 | エンジニアリング Issue からコード変更する |
| `no_change` | 修復後の絵コンテが前回と同一で進展なし | エンジニアリング Issue からコード変更する |
| `no_visual_change` | 絵コンテは変わったが清浄なメディアが同一で進展なし | エンジニアリング Issue からコード変更する |

- インフラ障害（malformed／stale／closed／source drift／機械 QA 失敗、モデルや commit の失敗）は上記 status とは別で、`studio_repair.py` は非ゼロで fail-closed に終了し、公開しない。
- `needs_engine`／`attempts_exhausted`／`no_change`／`no_visual_change` では publish-review が子ごとに1件だけ bot のエンジニアリング Issue を保証する。**エンジンのコードを自動で書くジョブはまだない**。実際のコードエージェントはその Issue から作業し、別のエンジニアリング PR を作る。その PR のレビューと Merge が終わってから、オーナーが元の音源 Issue で制作を手動 dispatch する。対応していない機能が自動で直るわけではない。
- bot が作ったエンジニアリング Issue はコード修正の引き継ぎに使い、新しい録音の制作受付としては扱わない。録音の受付はオーナーが作った親 Issue に限る。

## 制限と注意

- このリポジトリは公開されているため、Issue 添付の録音も公開アクセス可能になる。GitHub 側の添付上限は変更されうるため、数値は Issue フォームと GitHub の現行ドキュメントで確認する。コントローラの 512MB 上限は防御であり、GitHub の許可量ではない。
- **音楽の聴取は未検証**である。候補選択は DSP の音響ランキングであり、音楽的良否の証明ではない。端点の根拠と懸念は JSON に保存される。
- 描画は手続きプロトタイプであり、完成したアニメ作品ではない。Studio のフレームは QA 文字・マーカー・ロゴのないクリーンな描画で、プロトタイプ表記は provenance／storyboard のメタデータにのみ置く。実行できるのは場面ごとのパレット6色と4つの数値パラメータ（`figure_scale`、`particle_multiplier`、`light_bias`、`glow_scale`）だけで、`motif`／`camera` の説明文はプログラムとして実行しない。新しいデコード済み参照フレーム QA は符号化動画と決定的描画の機械的対応を確認するもので、美的・音楽的な真実の判定ではない。生成素材のプロバイダは将来の差し替え対象であり、時刻決定権は渡さない。
- 時期の解釈（early＝プロ演奏への憧れ、middle＝コンサートの存在感と表現、late＝癒し）は**創作上の方向付けであり歴史的・音楽学的事実ではない**。曲名・時期は任意で既定は unknown である。添付録音を特定の曲と同定しない。
- 60秒未満の音源は actionable なエラーで止まる（隠れた padding／stretch はしない）。
- 秒単位のイベントはネイティブサンプル／30fps フレームで検証する。固定 BPM グリッドは使わない。
- artifact は90日保持する。原録音の全体は Issue 添付と一時ワークスペースのみに存在し、git・plan artifact・Release には入れない。
- 自動マージ、bot の APPROVE、YouTube API、音声のモデル API 送信は行わない。モデルが見るのは有界な DSP JSON と画像証拠のみである。
- 復号はビット深度を保持する。WAV はネイティブ subtype／チャンネルを維持し、圧縮音源は32bit float WAV に復号する（無条件に pcm16 化しない）。初回 Muse はホストに音源をダウンロードする前に実行する。修復時の Muse は、ホスト上に音源 WAV が存在する間も、入力だけをマウントした隔離ワーカー（`muse-run.sh`）の中で実行し、原音源・信頼済みチェックアウト・GitHub トークンはワーカーへ渡さない。
- 今後は宣言的 storyboard に素材プロバイダを追加する。生成画像・短い生成動画は素材として受け取り、尺・カット・アクセントは既存のサンプル／フレーム基準で決める。現在の風景・人物シルエット・パレット制御で表現できない演技や物語は次段階の課題であり、音声の直接聴取や楽譜／MIDI 併用による候補選択の改善も別実験とする。生成 API の課金は承認前に行わない。

## ローカル検証（オフライン）

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/python scripts/studio_smoke.py --out output/studio-smoke
```

`studio-ci.yml` は Secret・モデル・個人音源なしで同じ tests と60秒以上の合成レンダリング smoke を実行する。

## エージェント失敗時の診断（安全な分類のみ）

Muse のコンテナ停止を確認できない場合は、認証の再暗号化・保存も停止します。実行中の処理と認証保存が競合するのを防ぐためです。Actions のエラーを解消し、必要なら再ログインして auth smoke を実行してから再試行します。


Codex planner／reviewer と Muse の失敗時は、捕捉した stderr を `.github/studio/scripts/report-agent-error.py`（標準ライブラリのみ）に渡し、固定の許可リスト分類と次の一手だけを Actions ログに出す。生の provider 出力・認証情報・プロンプト・入力行・パスは決して出力しない。診断は `|| true` でガードし、元のエージェントの非ゼロ終了コードを保持する。

| 分類 | 意味 | 次の一手 |
| --- | --- | --- |
| `expired_authentication` | `invalid_refresh_token`／`token_revoked` 等の認証失効 | 再ログインし暗号化認証を更新して auth smoke |
| `unsupported_model` | モデル未対応（HTTP 400 等） | ログの CLI バージョンとモデルの利用可否を確認して auth smoke |
| `usage_limit` | 利用上限／quota | 上限のリセット・引き上げを待って auth smoke |
| `sandbox_setup_failure` | sandbox セットアップ失敗 | runner の sandbox 設定・権限を確認して auth smoke |
| `generic` | 上記に当てはまらない不明な失敗 | auth smoke で切り分け（生出力は共有しない） |

API 仕様の根拠: [GitHub sub-issues](https://docs.github.com/en/rest/issues/sub-issues)、[Release 一覧と公開](https://docs.github.com/en/rest/releases/releases)。
