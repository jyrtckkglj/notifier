# ポケカ販売・抽選情報の通知ツール

ポケカの販売・抽選情報をまとめているサイトを1日1回確認し、
**ポケモンセンターオンライン** と **Amazon** の情報だけを抜き出して、
一覧ページ(`docs/index.html`)にまとめ、Discord に通知します。

## 情報源(config.json で追加・変更できます)

| サイト | 使い方 |
|---|---|
| [攻略大百科 ポケカ](https://premium.gamepedia.jp/pokeca/) | RSSで新しい「抽選・予約情報まとめ」記事を見つけ、記事内のショップ別の表からポケセン・Amazonの行を抜き出す |
| [ポケざんまい](https://www.pokeca-zanmai.jp/) | RSSの抽選記事に加えて、「Amazon招待リクエスト受付中の商品一覧」の表を見る |

- 記事は後から追記されるので、見つけてから45日間は毎日読み直します
- 同じ商品でも受付開始日が変われば(再販・2次抽選など)新着として通知します
- 1回の実行で数ページだけ、5秒以上の間隔を空けて読みます。ログイン・応募・購入はしません
- Python 3.9 以上だけで動きます(追加インストール不要)

## GitHub Actions で動かす(このリポジトリの設定)

1. Discord でチャンネルの「設定 → 連携サービス → ウェブフック」から Webhook URL をコピー
2. リポジトリの Settings → Secrets and variables → Actions → New repository secret で、
   名前 `DISCORD_WEBHOOK_URL`、値に Webhook URL を登録(暗号化され、公開されません)
3. Actions タブ → daily → Run workflow で初回実行(以降は毎朝8時に自動実行)
4. Settings → Pages → Source を「Deploy from a branch」、Branch を `main` / `/docs` にすると、
   一覧ページが `https://<ユーザー名>.github.io/notifier/` で見られます

## 自分のPCで動かす場合

1. Discord でチャンネルの「設定 → 連携サービス → ウェブフック」から Webhook URL をコピー
2. `config.example.json` を `config.json` にコピーし、URL を貼り付け
3. 試しに実行(初回は今ある情報を登録するだけで、通知は「開始しました」の1通のみ)
   ```
   python pokeca_notifier.py
   ```
4. `docs/index.html` をブラウザで開くと一覧が見られます

## PCで毎日自動で動かす

- Windows: タスクスケジューラで「基本タスクの作成」→ 毎日 → 操作に `run_daily.bat` を指定
- Mac / Linux: `crontab -e` に `0 8 * * * cd /path/to/pokeca-notifier && python3 pokeca_notifier.py >> run.log 2>&1`

## うまく取れないとき

`python pokeca_notifier.py --dump-html --no-notify` で取得したページが `debug/` に保存されます。
`--offline-dir debug` を付けると、保存したページを使ってネットに接続せずに試せます。
