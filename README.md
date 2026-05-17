# mirakosta-watch (GitHub Actions + メール通知版)

Hotel MiraCosta (DHM) のキャンセル空きを検出してメールで通知する。
GitHub Actions の `schedule` で5分間隔に実行(実際は10〜15分間隔のことも)。

## 構成

```
.github/workflows/watch.yml   # スケジュール実行 + state cache管理
mirakosta_watch.py            # 本体 (SMTP送信)
config.json                   # 監視対象URL/ラベル (非秘匿)
requirements.txt
.gitignore
```

- **シークレット**: SMTP接続情報 → GitHub Secrets
- **状態保存**: `state.json` を Actions cache で run間に引き継ぎ
- **コスト**: Publicリポジトリなら無料無制限

-----

## セットアップ

### 1. リポジトリ作成

GitHubで新規 **Public** リポジトリを作成し、このディレクトリの中身をpush。

```bash
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin git@github.com:YOUR_NAME/mirakosta-watch.git
git push -u origin main
```

### 2. メール送信用アカウントを準備

#### Gmail を使う場合 (推奨)

1. Googleアカウントで **2段階認証を有効化**
1. <https://myaccount.google.com/apppasswords> で **アプリパスワード** を発行
- アプリ名: 任意(例: `mirakosta-watch`)
- 16桁のパスワードが表示されるのでコピー
1. SMTP設定:
- host: `smtp.gmail.com`
- port: `587` (STARTTLS) または `465` (SSL)
- user: 自分のGmailアドレス
- password: 上で発行したアプリパスワード(通常のパスワードではない)

#### Outlook / 他のSMTPを使う場合

各プロバイダの SMTP 情報を使う。多くは port 587 + STARTTLS。

### 3. GitHub Secrets 登録

リポジトリ → Settings → Secrets and variables → Actions → **New repository secret**

|Name           |Value                |例                                       |
|---------------|---------------------|----------------------------------------|
|`SMTP_HOST`    |SMTPサーバ              |`smtp.gmail.com`                        |
|`SMTP_PORT`    |ポート番号                |`587`                                   |
|`SMTP_USER`    |ログインID               |`you@gmail.com`                         |
|`SMTP_PASSWORD`|パスワード(Gmailはアプリパスワード)|`xxxx xxxx xxxx xxxx`                   |
|`MAIL_FROM`    |送信者アドレス              |`you@gmail.com`                         |
|`MAIL_TO`      |受信先(カンマ区切り可)         |`you@gmail.com` または `me@a.com,you@b.com`|
|`SMTP_USE_SSL` |SSL直接接続(任意)          |port 465なら `true`、587なら省略/`false`       |

### 4. ワークフロー有効化

Actions タブを開いて **I understand my workflows, go ahead and enable them** をクリック。

### 5. 動作確認

Actions タブ → **MiraCosta Watch** → **Run workflow** を押し:

- `test_mail: true` → メールに「✅ テスト通知」が届く
- `debug: true` → HTMLスナップショットを artifact として保存

artifact をDLしてHTML中身を確認し、`AVAILABLE_KEYWORDS` / `UNAVAILABLE_KEYWORDS` が想定通り含まれるか確認。

### 6. 監視対象を変える

`config.json` の `target_url` の URLパラメータ(`useDate=20260917`, `adultNum=2`, `stayingDays=1` 等)を書き換えてpush。

-----

## 動作仕様

|状態遷移                       |メール通知        |
|---------------------------|-------------|
|INIT → UNAVAILABLE         |❌            |
|INIT → AVAILABLE           |✅            |
|UNAVAILABLE → **AVAILABLE**|✅ ← **本命**   |
|AVAILABLE → AVAILABLE      |❌(連投防止)      |
|any → UNKNOWN              |❌(誤検出回避、ログのみ)|

連投したい場合は `config.json` の `notify_on_unchanged_available` を `true` に。

メール本文には対象ラベル・検出キーワード・検出時刻・予約URLが含まれる。スマホでメール通知をONにしておけばプッシュ通知と同等に使える。

-----

## ローカル動作確認(任意)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export SMTP_HOST=smtp.gmail.com
export SMTP_PORT=587
export SMTP_USER=you@gmail.com
export SMTP_PASSWORD='xxxx xxxx xxxx xxxx'
export MAIL_FROM=you@gmail.com
export MAIL_TO=you@gmail.com

python3 mirakosta_watch.py --test-mail
python3 mirakosta_watch.py --debug
```

-----

## GitHub Actions の注意点

### スケジュール精度

`cron: '*/5 * * * *'` でも GitHub Actions の schedule trigger は**ピーク時に15〜30分遅延**することがある。リアルタイム性最優先なら自宅PC cronの方が確実。

### state.json の永続化

- 毎runで一意キー `mirakosta-state-${run_id}` で保存
- 次回run開始時に `restore-keys: mirakosta-state-` で最新の prefix一致 cache を引っ張る
- `cleanup-old-caches` ジョブで最新1件以外を削除して10GB枠を圧迫しない
- 7日間アクセスがないとGitHubが自動削除するが、5分おき実行なら問題なし

-----

## トラブルシュート

### Gmailで `Username and Password not accepted` エラー

- 通常のGoogleパスワードではなく **アプリパスワード** を使う
- 2段階認証が有効になっているか確認
- アプリパスワードはコピー時にスペースが入るが、貼り付け時は除去してもしなくても可

### `status=UNKNOWN` が連発する

Disney側のページがJS必須で `requests` では空室情報が取れていない可能性。`fetch_page` を Playwright 版に差し替える:

```bash
# requirements.txt に追加
playwright>=1.40.0
```

```yaml
# workflowに追加
- run: python -m playwright install --with-deps chromium
```

```python
def fetch_page(cfg, logger):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=cfg.user_agent, locale="ja-JP")
        page = ctx.new_page()
        page.goto(cfg.target_url, wait_until="networkidle",
                  timeout=cfg.timeout_sec * 1000)
        html = page.content()
        browser.close()
        return html
```

### cron が動かない

- Publicリポジトリで60日活動がないとscheduledワークフローが自動無効化される。手動 Run workflow か commit すれば活性化
- リポジトリのデフォルトブランチに `.github/workflows/watch.yml` がないと有効化されない

### メールが届かない / 迷惑メールに入る

- まずはActions のログで SMTP がエラーになっていないか確認
- Gmail → Gmail 自送信は迷惑メール判定されにくい
- 件名に絵文字 🎉 が入っているが、嫌なら `run_check` の `subject` を編集