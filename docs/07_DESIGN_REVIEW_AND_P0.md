# 07 — 設計レビューと P0 実測報告

作成日: 2026-10-19 / 最終照合: 2026-10-20。本書は日付のある P0 測定報告である。文中のテスト件数や数値は当時の証跡であり、現在の契約や最新の合格件数ではない。
対象: `assets/reference_audio.wav`（解析専用WAV、公開音源ではない。クローン直後には存在しない場合がある）
実行: `python -m p0.pipeline`（当時1コマンド、終了コード 0、PIPELINE PASS +
MANIFEST_VERIFY PASS、pytest 38件）

この文書は資料の要約ではなく、**何が壊れうるかを優先順位付きで列挙し、
当時何を実測で潰したか・何が未検証に残るかを区別する**ための記録である。
曲名・感情・フレーズの正解は断定しない。音楽的境界の正解・美的優劣は
人による評価が未実施のため断定しない。
文中の `output/` 証跡パスはローカル生成物で git 管理外であり、クローン直後には存在しない。

## 0. 外部情報の現行確認（親エージェント調査、2026-10-19公式ページ）

実装方針を拡大せず、出典付きで制約だけを取り込む。

- Remotion料金: 個人・3人以下の企業は無料の商用利用対象、4人以上の組織等は
  会社ライセンス（[公式料金](https://www.remotion.dev/docs/license/pricing)）。
  「商用だと常に有料」と記載しない。
  [LICENSE](https://github.com/remotion-dev/remotion/blob/main/LICENSE.md)は
  5.0で変更予告あり。今回は未採用で課金なし。
- FFmpeg: 基本LGPL 2.1+、GPL部品を使うビルドはGPL
  （[公式](https://ffmpeg.org/legal.html)）。今回binaryの `-buildconf`
  （`--enable-gpl` 含む）を `run_manifest.json` に記録。
  ローカル動画制作と将来のソフトウェア再配布の条件は区別する。
- YouTube生成/改変コンテンツ開示: 現実的な生成/改変映像・生成音楽は開示要、
  非現実的アニメ等は開示不要例あり、開示自体は収益化資格を損なわない
  （[公式](https://support.google.com/youtube/answer/14328491?hl=en)）。
  実演者の写実合成は別途判断。今回の公開可否・曲名・権利確認は未実施、投稿なし。
- YouTube量産型コンテンツ: 独自性に乏しいテンプレ/量産は問題視
  （[公式](https://support.google.com/youtube/answer/1311392?hl=ja)）。
  本人音源でも収益化は保証されない。作品ごとの実質差で評価する。
- Novelty/SSM: chroma等のself-similarity/novelty変化点検出の解説
  （[FMP C4S4](https://www.audiolabs-erlangen.de/resources/MIR/FMP/C4/C4S4_NoveltySegmentation.html)）。
  ただしkernel幅/peak picking依存で小kernelは偽ピーク増。
  新実装はSSMではなく左右特徴ベクトル比較（lagged cosine）であり、
  その通り記載してSSM実装と誇張しない。
- librosa onset center: 旧版docは `center=True` で
  `n_fft//(2*hop_length)` frame shiftとあるが
  （[旧doc](https://librosa.org/doc/0.11.0/generated/librosa.onset.onset_strength.html)）、
  現環境1.0.0に鵜呑みにせず installed code＋合成attackで検証する方針を維持。
  旧scriptのn_fft=2048@11025はwindow約186ms・hop約23ms（hop分解能≠検出精度）。
  旧RMSのsigma1.8sはwindow長ではなくGaussian sigma（前後数秒を混ぜる）。

## 1. 確定事項 / 仮説 / リスクの切り分け

- 確定（実測）: 原音の物理量（22050Hz mono PCM_16、3685555 samples、
  167.145351s、sha256 `a4c2e99c…1c20cd15e`)、旧解析の再実行一致（§4、
  参照音源のみ適用）、整数演算の時刻変換と逆写像の全数検証（§5①）、
  真RMS・パディング・chroma power入力の明示と合成検証（§5②③⑯）、
  frame上2D-DPと端点候補選択（§5④⑤）、cropのPCM完全一致（§6）、
  1frame閾値の音声QA＋marker前後検証＋negative tests（§6）、
  終了後manifest再検証（§6）、2本のMP4の機械QA全PASS（§6）。
- 仮説（未検証）: 音響候補が「音楽的に自然な」カット位置と一致すること、
  プレースホルダーの見た目が視聴者に受け入れられること、週2本回せること。
- リスク（残存）: §2 の破綻仮説リスト。特に R-1（構造候補と音楽的境界の
  乖離）は P0 では原理的に潰せない。新規 R-8（正規化noveltyの微小変化増幅）
  を追加。

## 2. 破綻仮説（優先順位付き）

| # | 仮説 | 優先度 | 現状 |
|---|---|---|---|
| R-1 | 音響的な構造候補が、聴感上のフレーズ境界とずれる（ペダル・ルバート・転調なしの強弱変化で誤配置） | 高 | 未検証。聴取による比較が必要 |
| R-2 | 音域移動を音色変化と誤認する（centroid/rolloff は両方に反応。第3優先以下では未分離） | 高 | 方向性のみ試験（高音域だけの変化が centroid を動かすことを確認）。分離は未実装 |
| R-3 | onset/flux がレガート・弱音・密集和声を取りこぼし、アクセント層がスカスカ/過密になる | 高 | 合成信号では検出。実演の弱音連打での取りこぼし率は未測定 |
| R-4 | クリップ自動選択が凡庸な区間（冒頭の静寂など）を選ぶ | 中 | 改訂後は `[1521450, 2362290)`=69.0〜107.1s・38.13sを選択（始端grid・終端music-endpointという内部ラベル。音響候補近傍、attack近接ペナルティ0）。理由と根拠は `analysis.json/clip_selection` に保存したが音楽的良否は未判定。終端後にも音が続く（§11） |
| R-8 | 正規化noveltyが微小なchroma振動を増幅し、音高不変の変化（octave shift合成でraw 0.0025）にもstruct候補を出す | 中 | 実測で確認（真のpitch-class変化raw 0.19の76分の1でも正規化後は同strength 0.7）。絶対フロアは未検証のため入れず、限界として明記。単一cue減点（×0.7）のみで緩和 |
| R-5 | プレースホルダー画が「安っぽい量産映像」に見える | 中 | 同期用の2D仮素材。親の静止画目視評価を文書09に記録。物語・人物演技・演奏者の識別要素は未実装。視聴によるAB比較は未実施 |
| R-6 | AAC エンコードやコンテナ丸めで AV がずれる | 低 | 実測で潰した（lag 0、相関 0.9999、§6） |
| R-7 | 同 seed でも環境差で analysis/plan が変わる | 低 | 同一環境での再現性は試験済み。ライブラリ更新時の差は `run_manifest.json` の versions+hash で検出可能 |

## 3. 3案比較と今回の選択

| 観点 | A naive（RMS直結） | B structured（今回採用） | C semantic（AI説明付き） |
|---|---|---|---|
| 時間同期の検証可能性 | 可（ただし過敏） | 可（疎＋抑制） | B に依存。LLM時刻は採用しない |
| 音楽的自然さ | 期待薄（対照群） | 未検証（人比較が必要） | 未実装。解釈の揺れが評価対象 |
| 生成API費（開発・計算費を除く） | 0 | 0 | API費・承認フローが必要 |
| 失敗時の挙動 | 常に動く | fallback で1シーン化 | B に fallback |

選択理由: P0 の問いは「手調整ゼロで同期が成立するか」であり、意味解釈は
混ぜない。C は時刻根拠を持てないため P0 の対象外とし、
B と対照群 A を同一クリップ・同一 seed で比較できる形にした。
描画方式（Python/PIL + FFmpeg）は「環境で最も簡単に動くもの」として選択し、
最終採用とは断定しない。`analysis.json` / `plan.json` / 制御配列（npz）が
データ契約となり、Remotion/Blender への将来移行はこの契約越しに可能。

## 4. 旧解析の実測監査（`output/legacy_audit.json`、参照音源のみ適用）

- 他音源（無音等）では比較対象が存在しないため適用外として記録し、
  正常な旧監査として扱わない。パイプライン自体は通過する。
- 旧スクリプト・旧 JSON は無改変。再実行（librosa 1.0.0 / numpy 2.5.4 /
  scipy 1.18.1）は `legacy_recomputed.json` に新規出力。
- ピーク順位・時刻は8件すべて一致（153.0 / 54.0 / 28.2 / 121.6 / 98.9 …）。
- 5秒窓の最大絶対差は 0.0（小数3桁の丸め済み値で完全一致）。
- 5秒窓は33個で165秒まで。末尾約2.15秒を覆わず、5秒窓や秒単位の平滑化値を精密なカット時刻に直接使わない。
- duration 表示差の説明: 旧スクリプトは 11025Hz リサンプル後の
  `len(y)/sr = 167.14539683` を表示するが、ファイル真値は
  `3685555/22050 = 167.14535147`。46µs 差はリサンプルの端数であり、
  ファイルの破損ではない。
- ただし一致は「同じコードを同じ条件で回した」ことに過ぎず、旧 `activity`
  指標が音楽的意味を持つ証拠にはならない（音量＋onset の手製合成である）。

## 5. 同期テストと数値結果（時刻は整数 sample / frame が基準）

- 時刻基底: `p0/timebase.py`。半開区間 `[start, end)`、丸めは**整数演算の
  round-half-up**（floatを経由しない）。逆写像 `sample_to_frame` は
  `(den*(2*rel+1)-1)//(2*num)` の厳密な逆写像であり、旧float閉形式が
  有理グリッドのtieで外しうる問題を解消。8種のsr/fps（22050/44100/48000/
  8000/96000 × 30/30000-1001/24000-1001）×開始3通り×2000frameで
  `sample_to_frame(b(i))==i`・`(b(i)-1)==i-1` を全数検証し、
  先頭6frameの全sample帰属も検証（`tests/test_timebase.py`）。
  非対応入力（非正・非整数）は reject。
- RMSとpadding: `rms` は真の時間領域RMS（矩形窓・frame 2048・hop 512・
  center=True・constant zero padding、波形振幅単位・gain線形）。STFT系も
  `pad_mode='constant'` を明示（installed librosa 1.0.0のdefaultを確認。
  旧metaのreflect記載は誤りだった）。chromaにはpower spectrum
  `|STFT|**2` を渡す（librosa仕様どおり）。正弦0.4→rms平均0.2828±0.01、
  ×4 gainで厳密4倍、無音で全0を試験。
- 中心/遅延: STFT center=True（frame i の中心 = i×hop、両端constant）。
  fluxは隣接STFT frameの差分であり、hop幅は精度保証ではない。
  合成impulseで検出誤差 68 samples（≦hop 512）を実測（`qa.json/synth_probe`）。
  最近傍hopグリッドの量子化幅（半hop=256 samples）と実際の検出誤差を区別する。
  harm_noveltyはlag約2.0s（86 frames）の比較区間の**整数中点**を代表時刻にする。
  初期の右端時刻は合成信号に約1秒の遅れを生じたため変更した（§11）。
  比較区間は真の境界の信頼区間ではなく、平滑化・多声部による不確実性は残る。
- 合成ケース: ピアノ風 attack（[34, 341] samples、±hop 以内を `qa.json` に記録）、
  gain ステップ（±0.15s 以内）、無音→flat かつイベントゼロ、
  微小音（1e-6）→flat、長い sustain（8秒で accent ≦2）、
  rubato 間隔（0.5/0.9/0.6s）の全 attack 検出、高音域だけの変化が
  centroid/band-high を rms より大きく動かすこと（方向性のみ、分離未実装）。
  エネルギー一定の和音変化（C→F#、0.5s crossfade）で harm_novelty 候補が
  出ること、octave shift（+12st）のraw novelty が pitch-class変化の
  10分の1未満であること（実測76分の1、方向性のみ、閾値化はしない=R-8）。
  無音gateとenvelope-flatは分離（真無音のみ候補ゼロ）。
- 描画到達検証: 8×8 診断マーカー（R=scene、G=accent onset edge、B=frame mod
  256）を実 MP4 からデコードして読む。accentは `G(f-1)<90 → G(f)>120` の
  単フレームエッジ、scene境界は `R(f-1)→R(f)` の厳密ステップで検証し、
  +1frameずらしplanが不合格になることをnegative testで証明。
  Bは粗いframe identity（±12、大きなずれ・落丁検出用）。
  H264量子化の最大絶対偏差は7（9probe中、8bit値）。
  plan の検査ではなく符号化済み画素の検査。マーカーは tiny。
- pytest 38件全PASS（`tests/`）。パイプライン内からも実行し `qa.json` に記録。
- 旧DPの反例: MAX 2 cuts・位置[0,6,12,18,36]s・重み[0,.2,10,10,0]で
  単一経路DPは10.1に留まり最適{B,C}=19.9を見失う。新2D-DPは一致し、
  60件のランダム小規模全探索とも一致（`tests/test_clip_dp.py`）。

## 6. 実ファイル QA の数値（`output/qa.json`、両 MP4）

- frame count 1144/1144、duration 38.1333/38.1333s。video/audio stream別に
  start（許容1frame=0.0333s）・duration（許容±1.5フレーム）を検査。
  AV start 0.0/0.0s、h264+aac、540×960。
- デコード音声 vs 原音 crop: 探索4096 samplesに対し合否閾値は映像1frame
  （735 samples、独立設定で循環合格なし）。lag 0（全区間＋head/mid/tail
  各区間、drift 0）、peak_corr 0.9999、level_diff -0.021dB（閾値±1.0dB）。
  AAC損失を許容し完全一致は要求しない方針どおり。
- crop PCM完全一致: PCM_16 roundtripで原区間との差は全sample 0 LSB
  （`crop_pcm_exact`、両mode）。解析用mono downmixの混入なし。
- 黒フレームなし（intro 0.5s以降のblack 0秒。全編blackはblack_endで検出）。
  内容変化あり（structuredの中央crop＋blur＋downscale差分 2.05〜6.96、floor 0.4。
  凍結+grain合成は0.05〜0.28で不合格になることを別途試験）。
- シーン不変条件（整列・最小尺6秒=180frame・隙間/重複なし・全被覆）PASS、
  入力 hash 不変、診断マーカー PASS（scene境界ステップ＋accent onset edge、
  前後frame含む。量子化最大偏差7）。
- クリップ `[1521450, 2362290)`（38.1333s、1144フレーム、69.0〜107.1s）、
  シーン3（0–413 / 413–807 / 807–1144 フレーム）、accent 9/158 採用
  （min gap 2s、strength floor 0.5、cap 10）。fallback なし。
  端点根拠: 始端grid・終端music-endpoint、両端とも音楽端点近傍で
  attack近接・大音量edgeなし（`endpoint_basis/concerns` に記録）。
- 再現性: 同一入力で分析/計画/controlを2回計算し完全一致
  （`qa.json/reproducibility.recompute_match=true`、MP4バイト列は除外）。
- manifest再検証: 全工程終了・ログclose後に全成果物hashを再読検証
  （`output/manifest_verify.json`、verified=true）。manifest/log自身は
  自己参照のため除外（明記）。再実行コマンドは
  `python -m p0.pipeline --audio … --out …` 形式で保存。
- 無音入力の省演出フォールバック: 40s無音WAVを低解像度（180×320・別out）で
  CLI通過を確認。1シーン・accent 0・legacy監査適用外（記録のみ）・
  音声QAは両側無音N/A一致でPASS停止せず・映像はdimだがblackなし・
  motion 1.2〜2.2で内容変化PASS。PIPELINE PASS＋MANIFEST_VERIFY PASS。
  `features_uri` はout外でも壊れない（`features.npz`＋`out_dir` base記録）。
- 手作業タイムライン修正件数: **0**（パイプラインは cut 秒の手入力を受けない）。
- `cost.json`: 生成API 0件・0USD。muse 開発費は別管理で unknown、電力は
  未計測（ゼロと混同しない）。
- 再現性: 同一入力・同一 seed で analysis/plan/control が一致することを
  テストで確認（MP4 バイト列はコーデック差がありうるため対象外）。

## 7. 検証済みと未検証の境界

- 検証済み（機械）: 上記 §4–§6 のすべて。`PIPELINE PASS`（exit 0）。
- 未検証（人）: 音楽的自然さ（カット位置・演出量が曲想に合うか）、
  プレースホルダーの美しさ・世界観、複数音源での汎化、週2本の制作時間、
  視聴者指標（維持率・登録転換・実演への流入）。動画が完成したことをもって
  音楽的自然さを合格扱いしない（`qa.json` に明記）。

## 8. 次の壁打ちへの回答（具体案）

- RMS 以外の組み合わせ: 現状は slow_env（照明/粒子）＋ chroma novelty
  （構造候補の支持 cue の1つ）＋ flux（疎 accent）。次は band-low の
  「低音の動き」を独立 cue にし、各 cue の寄与を ablation（抜き差し比較）
  で測る。重みは固定値のまま、人評価とセットでしか変えない。
- rubato / pedal / polyphony の限界: 固定BPM を捨てた現設計でも、ペダルで
  伸びた残響は flux を鈍らせ、密集和声は chroma を壊す。対策は「検出の
  改善」より「信頼度が低い区間では演出を薄くする」fallback を優先する。
  低 strength 区間の演出抑制は control 側に実装済みの方向（hysteresis+slew）。
- score / MIDI 導入の判断条件: 同一演奏の MIDI（または採譜）が手に入り、
  かつ音響のみの構造候補と人評価で 3 本以上乖離が確認できたら導入を検討。
  導入しても時刻の真値は音源 sample のままにし、MIDI は支持 cue に留める。
  重い全曲採譜は P0 の次でも必須にしない。
- cut 最適化: 2D-DP（§`p0/clip.py`: 候補は構造境界＋谷をclip frameに
  snap後に確定、制約は最短6秒=180frame・最大5カット・strength floor 0.15、
  状態はdp[node][cuts_used]、目的は支持合計−0.05×カット数）は小規模で十分。
  3〜6シーンは目標であり、弱い根拠での強制はしない（今回は3シーン、
  fallback なし）。次は「カット位置の人評価」を目的関数の外側で測り、
  floor/penalty を調整する。clip端点はdip/struct由来＋grid fallbackの
  両プールから選び、端点根拠・懸念をJSON保存するが音楽的正解は断定しない。
- 生成動画の時間制御の限界と overlay 方針: 生成動画クリップは内部時刻を
  正確に制御できない前提を維持する。精密同期はレンダラー側の
  overlay/effect/cut 境界でのみ保証し、生成素材は.fill（尺合わせは
  トリム・ループ・ extremely 遅い crossfade のみ）とする。タイムストレッチ
  で音に合わせる方向は採らない（演奏の時間を壊すため）。
- 自動失敗検出と fallback: flat 入力→イベントゼロ→1シーン＋演出最小化、
  構造候補ゼロ→DP が自動で1シーン化、accent ゼロ→accent 層 off。
  いずれも理由を JSON に残し exit 0（縮退は失敗ではない）。QA 不合格
  （AV 不一致・黒・静止・hash 変化）のみ exit nonzero。
- 人が残す判断（低頻度）: 世界観・画風・公開可否・選曲。毎回の秒指定は
  受け付けない（CLI に cut 秒の入力口がない）。
- 次実験: (a) 異なる演奏3本での同一パイプライン実行と破綻の分類、
  (b) naive vs structured の AB ブラインド比較（評価者は音楽的自然さのみ採点、
  どちらがどちらかを知らせない）、(c) 構造候補と人指定境界の一致率測定
  （人には秒を「評価」として付けてもらい、「入力」にはしない）。
- AI 統合時の境界: 演奏音源・原動画・そのフレームをモデルや外部サービスに
  添付しない（今回も遵守）。P1 の LLM は構造候補の「読み替え提案」に限定し、
  時刻の採用は検出結果＋制約で検証する。外部送信・課金 API・投稿は
  ユーザーの明示承諾なしに実行しない。予算上限・失敗時 fallback・権利と
  来歴のロギングは API 導入の前提条件。

## 9. 成果物一覧（当時のローカル出力。`output/` は git 管理外でクローン直後には存在しない）

- `output/poc_structured.mp4` / `output/poc_naive.mp4`（540×960、30fps、38.1333s）
- `output/analysis.json` / `output/plan.json` / `output/plan_naive.json` /
  `output/storyboard.json` / `output/qa.json` / `output/cost.json`
- `output/timeline.png`（clip終端も明示） / `output/timeline.html`
  （外部通信なし、両mode切替・playhead・exact local/source時間・click-to-seek。
  seek補正なし、動画max-height 65vh）
- `output/run_manifest.json`（再実行コマンド・コードhash・入力hash・seed・
  versions・ffmpeg buildconf・工程時間）/ `output/manifest_verify.json`
- `output/run.log` / `output/features.npz` / `output/control_*.npz` /
  `output/clip_audio.wav`（subtype/channel保持のlossless crop、output 内のみ）/
  `output/thumbs/` / `output/qa_dec_*.wav`（デコード比較用中間物）
- `output/legacy_audit.json` / `output/legacy_recomputed.json`
  （参照音源のみ適用。他音源では適用外を記録）
- 実装: `p0/*.py`（timebase/features/events/clip/plan/render/qa/viz/pipeline）
- テスト: `tests/`（38件: timebase 7・features 15・clip DP 4・render 2・
  determinism 1・QA negatives 6・structure midpoint 3）
- 再現用依存: `requirements-p0.txt`（固定版。README手順はこれを入れる）、
  環境: project `.venv`
- 映像内captionは短いASCIIのみ（`P0 <mode> placeholder i/n`）。
  日本語captionは既定fontにglyphがなく欠字になるため使わない。
  夜景はプレースホルダーとして成立するがAIアニメ作品品質とは報告しない。

## 10. 未達・リスクの正直な記録

- クリップの音楽的良否は未判定（R-4として残す）。端点根拠・懸念はJSON化
  したが、人が「自然な抜粋」と評価した事実はない。
- 解像度は 540×960（PoC 許容）。1080×1920 は未検証。
- semantic（C案）は未実装。chroma は候補 cue であり和声理解ではない。
  octave不変性の方向性は合成でのみ確認（R-8の限界つき）。
- 人による音楽的評価は未実施（0件）。手作業タイムライン調整も 0 件。
  この2つのゼロは意味が違う（前者は未達、後者は達成）。
- AAC priming由来の系統lagは今回0で問題化しなかったが、別環境・別音源で
  出た場合は閾値（1frame）で不合格になる設計。黙って緩めない。
- ステレオ音源のcrop保持は実装済みだが実ステレオでのCLI通過は未検証。
  P0の検証済み入力はmono PCM_16のみ。

## 11. 微小タスク: chroma-novelty中点時刻（2026-10-20）

- 背景: 親の独立校正（[08](08_INDEPENDENT_SIGNAL_REVIEW.md)、
  `output/independent/structure_latency.json`）は、6.123秒中央・幅200msの
  equal-power triad変化に現実装が7.128526秒候補（約+1.0055秒）を返すことを示した。
  これは旧右端定義と整合する。描画同期PASSで隠さず、検出量の時間座標として扱う。
  親の元測定JSONは上書きしていない。
- 変更（定義のみ、固定秒の引き算補正ではない）:
  `harm_novelty[i]` の比較区間 `[start,end]=[(i-lag)*hop, i*hop]` と整数中点
  `mid=(start+end)//2`（t-lag/2）を明示し、novelty curveとイベント時刻を中点に統一。
  slow_edge・flux attackは右端のまま一段もずらさない。
  重み・描画/スタイルの再設計なし。
  変更: `p0/features.py`・`p0/events.py`・`p0/viz.py`・`p0/plan.py`・
  `p0/pipeline.py`（端点RMS）・`p0/clip.py`（fps厳密化）。
  回帰: `tests/test_structure_midpoint.py`（3件）。
- 校正（`output/structure_calibration.json`、手順 `scripts/calibrate_structure_midpoint.py`）:
  中心4.5/8.0/12.0秒（6.123秒を避けて過適合防止）、lag 0.5/1/2秒、gain 0.5x/2.0xの18件で
  旧右端と中点を同一ピーク枠で比較。
  結果: mean|new|=0.0185秒 vs mean|old|=0.6027秒、
  max|new|=0.0279秒 vs max|old|=1.0264秒。全18件・全lagで中点が改善したため中点を採用。
  合成triadの改善であり、実演のフレーズ精度は保証しない。
  Negative: 一定音はharm候補なし（始端過渡のslow_edgeのみ、実利用の3秒marginで除外）、
  無音は候補なし。Limitation: 同和音octave移動はraw約0.0025でも正規化でstrength 0.7
  （R-8維持）、crossfade octaveの時刻は不安定。
- 新clip端点RMS（今回、[08](08_INDEPENDENT_SIGNAL_REVIEW.md)§1の初回35秒終端値とは別物）:
  clip `[1521450, 2362290)`＝69.0〜107.1333秒、1144フレーム/38.1333秒（中点化後も同一clip）。
  `plan.json clip.edge_100ms_rms_dbfs` は始端前-39.86・始端後-39.69・
  終端前-42.89・終端後-40.23 dBFS。低エネルギーは終止の根拠にしない。
  終止の自然さは人未評価のまま明示。終了秒のハードコードなし。
- fps: 最小`ceil(30*fps)`・最大`floor(45*fps)`に厳密化（有理fpsで実durationが範囲内）。
  30fpsでは900/1350枠で従来と同一。今回1144フレーム/38.1333秒は範囲内。
- 再実行: `.venv/bin/python -m p0.pipeline` の1コマンドで両MP4・analysis/plan/timeline/qa/manifestを更新。
  PIPELINE PASS＋MANIFEST_VERIFY PASS、pytest 38件、recompute True。
  入力hash `a4c2e99c…1c20cd15e`（3685555 samples）不変、manifest hashes一致。
- 残存制約: 中点は検出量の座標定義であり音楽的境界の正解化ではない。
  平滑化・多声部・ペダルで真の境界は不確実（R-1残存）。R-8残存。終止の人評価未実施。
- 一般化の限界（2026-10-20追記）: 18条件の最大28msを他サンプルレートや
  実演へ一般化しない。親の追加測定（`output/independent/final_check.json`）
  では同一和音fixtureの別lag/時刻/ゲイン条件で22,050Hz最大約32ms、
  44,100Hz最大約102msが残った。今回はこの追加誤差の解析再設計をせず、
  限界として明示する。技術方式比較・次段の受け入れ条件は
  [09](09_ARCHITECTURE_DECISION_AND_ACCEPTANCE.md)。
