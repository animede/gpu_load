# GPU Pulse

Linux に搭載された GPU の負荷、VRAM、温度、消費電力などをリアルタイム表示するローカル Web アプリです。外部ライブラリやクラウド接続は不要で、取得したデータはマシン外へ送信しません。

## 対応

- NVIDIA: `nvidia-smi`（負荷、VRAM、温度、電力、クロック、ファン）
- AMD / Intel: Linux DRM sysfs（ドライバーが公開している負荷、VRAM、温度、電力など）
- 検出した GPU が2台以上なら自動でデュアル表示（手動で単一 GPU 表示にも切替可能）
- 1 / 2 / 5 秒の更新間隔、グラフの一時停止

Python 3.10 以降を想定しています。

## 起動

```bash
./run.sh
```

ブラウザで <http://127.0.0.1:8765> を開きます。別のポートを使う場合:

```bash
./run.sh --port 9000
```

LAN 内の別端末から見る場合は、アクセス制御とファイアウォールを確認したうえで次のように起動します。

```bash
./run.sh --host 0.0.0.0
```

実機 GPU がない環境ではデモモードで画面を確認できます。

```bash
./run.sh --demo
```

## テスト

```bash
python3 -m unittest discover -s tests -v
node tests/ui_smoke.js
```

## トラブルシューティング

- NVIDIA GPU が表示されない: ターミナルで `nvidia-smi` が成功するか確認してください。
- AMD / Intel の項目が `—` になる: 使用中のカーネルドライバーがその sysfs カウンターを公開していない場合があります。
- ポートが使用中: `./run.sh --port 9000` のように別ポートを指定してください。
