# 旧音響解析の再実行

元の特徴量解析を移植した参照スクリプトです。PoC本体のコードではなく、旧解析の実験再現用です。

```bash
python -m pip install numpy scipy librosa
python scripts/analyze_reference.py --audio assets/reference_audio.wav --out output/features_recomputed.json
```

出力ディレクトリを自動生成しないため、必要なら `mkdir -p output` を実行してください。

`reference_features_v0.json` と再計算結果は、環境やライブラリのバージョン差で完全一致しない場合があります。音源長、ピーク上位の傾向を比較してください。

# P0 パイプライン（本体。旧解析スクリプトは監査対象であり改変しない）

```bash
.venv/bin/python -m p0.pipeline   # 旧解析の再実行＋比較（output/legacy_audit.json）を含む
```

モジュール構成（`p0/`）: `timebase`（整数sample/frame基準、秒は表示用）、
`features`（RMS/flux/centroid/rolloff/band/chroma・低速novelty・dip）、
`events`（強度つき候補。未校正スコアを confidence と呼ばない）、
`clip`（30〜45秒の自動選択＋DPによる疎カット）、`plan`（JSON群＋制御配列）、
`render`（決定的2Dプレースホルダー＋FFmpeg）、`qa`（実ファイル検査。
不合格は exit nonzero）、`viz`（timeline.png/html）、`pipeline`（1コマンド実行）。
テストは `tests/`（`.venv/bin/python -m pytest tests -q`）。
