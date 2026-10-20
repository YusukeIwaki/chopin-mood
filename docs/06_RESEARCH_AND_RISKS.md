# 06 — 技術・権利・品質の未検証事項

## 参照すべきドキュメント（2026-10-19時点で確認した入口）

以下は参照時点の入口であり、現在の法的保証ではない。利用条件やポリシーは利用時に現行版を確認する。

### 音響 / 音楽解析
- librosa API: https://librosa.org/doc/latest/api/
- librosa onset/beat: https://librosa.org/doc/latest/auto_tutorials/01-intro/06-rhythm.html
- Spotify Basic Pitch (multi-pitch transcription): https://github.com/spotify/basic-pitch

### 音楽連動映像
- Remotion 音声可視化: https://www.remotion.dev/docs/audio/visualization
- Remotion `useWindowedAudioData`（バージョンにより対応コーデックが異なる）: https://www.remotion.dev/docs/use-windowed-audio-data
- FFmpeg docs: https://ffmpeg.org/documentation.html

### YouTubeのポリシー
- 収益化と画一的・量産型コンテンツの扱い: https://support.google.com/youtube/answer/1311392?hl=ja
- 生成/改変コンテンツの開示: https://support.google.com/youtube/answer/14328491?hl=ja

## リサーチ課題（Codexが実装時に現行仕様を確認）

1. Remotionのライセンス・商用利用条件とCLIレンダリング環境。
2. librosa/Essentia/AMT等のモデル、商用ライセンス・性能・メンテ状況。
3. 動画生成API各社の API可用性/秒単価/利用規約/生成物の権利/データ取り扱い/映像長/一貫性。特定モデルを予め指定しない。
4. LLMが音源を直接入力して音楽の意味や区間を返す機能と精度。ただし精密タイムコードの根拠にはしない。
5. YouTubeのAI表示・コンテンツID・著作権・収益化審査の現行規定。

## 重要なリスクと回避案

| リスク | 対応案 |
|---|---|
| RMSに連動するだけの安直なエフェクトになる | 意味のある構造境界/遅い演出/小さなアクセントを別レイヤーにする |
| ルバートによってbeat trackingが誤動作する | 固定BPM前提を廃し、onset/novelty/slow envelope中心のfallback |
| 音域の高さを音色の明るさと誤認 | クロマ/基音・周波数帯の補正や、関数の重み付けを比較 |
| シーン計画が音源に合わない | LLMを構造化出力に制限し、秒数・シーン数・最短尺を検証 |
| 同じ主人公なのに顔や衣装が変わる | `visual_bible`、参照画像、アセット履歴、生成モデル比較 |
| 映像生成動画が音に同期しない | AIに精密同期を求めず、決定的レンダラー/追加エフェクトで一致 |
| 生成APIコストが予想以上 | P0はAPIなし、予算上限、素材再利用、生成失敗時のfallback |
| AI素材が量産テンプレに見える | 曲・演奏解釈・物語・実演のオリジナル要素を重視 |
| 収益化や権利のトラブル | 商用ライセンス確認、演奏録音の権利、履歴と出典を保存 |
| コンサート録音で観客雑音がある | 雑音推定とノイズ低減を別処理とし、演奏音源を勝手に差し替えない |
| 映像の手の動きと音の時刻がずれる | 実写は演奏動画の音トラックを同期基準にし、編集時のオフセット検証 |

## 調べなくてもよいこと / 優先を下げること

- いきなりクラウドワーカーやSaaSのアカウント機能を開発すること。
- 全曲の完全な自動採譜をプロジェクトの前提にすること。
- 毎回新しい風景を生成するだけのチャンネル最適化。

## 設計仮説（要検証）

現実的な初期方式の仮説は **「音声解析で制御曲線とイベントを生成 → 物語/シーン割りを制約付きで決定 → 自動的に静止画を動かし、必要なら一部だけ生成動画を使う → レンダラーが時間整合を保証」**。ただし音楽的自然さは実作品で検証してから採否を決める。
