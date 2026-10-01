# 技術仕様

Flaskの画面・APIと、シフト・給与の計算を共通に使い、外部通信とセッション保存を実行環境ごとに切り替えます。ローカル版は単一プロセスのメモリ、Workers版はセッションごとのDurable Objectsで状態を管理します。

## 目次

- [アーキテクチャ](#アーキテクチャ)
- [ログインとらくしふ通信](#ログインとらくしふ通信)
- [実行環境ごとの処理](#実行環境ごとの処理)
- [月別キャッシュと画面更新](#月別キャッシュと画面更新)
- [認証とデータの保持](#認証とデータの保持)
- [HTTP API](#http-api)
- [データモデルと表示範囲](#データモデルと表示範囲)
- [用語](#用語)

## アーキテクチャ

| 層・入口 | 責務 | 主なファイル |
| --- | --- | --- |
| domain | 時刻、シフト、メンバー、閲覧者のモデル。休憩・勤務時間・重複・給与の計算 | `app/domain/models.py`、`services.py` |
| application | ログイン、日別・月別・メンバー別の参照、検索、給与計算の実行、月別キャッシュ | `app/application/use_cases.py` |
| ports | 認証・接続・セッション保存に必要な操作のインターフェース | `app/application/ports.py` |
| infrastructure | らくしふ認証、HTTP通信、外部JSONの変換、保存方式と試行制限 | `app/infrastructure/` |
| web | HTTP入力の検証、アプリCookie、認証判定、画面・JSON応答 | `app/web/routes.py` |
| ローカル入口 | Flaskの開発サーバーを起動 | `api.py` |
| Workers入口 | FlaskをWorkersのWSGIアダプターへ渡し、Durable Objectsを定義 | `worker.py` |

依存方向は`web / infrastructure → application → domain`です。アプリケーション層は外部依存の操作をポートとして扱うため、テストでは通信とセッションを代替実装に置き換えます。

フロントエンドは`templates/index.html`と`static/app.js`、`static/style.css`です。画面データを同一オリジンのAPIから取得します。Bootstrap 5.3.0とBootstrap Icons 1.11.3はjsDelivrから、InterとNoto Sans JPはGoogle Fontsから読み込みます。これらの表示資産の取得には外部通信が必要です。

## ログインとらくしふ通信

ログインは次の順に処理します。ローカル版とWorkers版で、参照先と取得内容は共通です。

1. `POST /login`でJSONの従業員ID・パスワードを受け取り、入力と試行回数を検証する。
2. `https://api.accounts.rakushifu.com/sign_in_with_employee_code/browser`へ、企業コード`skylark`と認証情報を送る。
3. `https://skylark.enterprise.rakushifu.com/authenticated_users`へ移動し、企業側の認証Cookieを取得する。`role=staff`、`enterprise_code=skylark`を指定する。
4. `/ajax/organizations`の`current_user`から、スタッフID、所属店舗ID、所属業態IDを取得する。
5. 日本時間の当月シフトを取得する。
6. ランダムなアプリ用トークンを生成し、セッションを保存してブラウザへCookieを返す。

当月の取得に失敗した場合は、アプリのログインを完了しません。ログイン時の当月判定はUTC+9、画面の初期月と今日の表示はブラウザの日時を使います。

シフトは`https://skylark.enterprise.rakushifu.com/ajax/admin/v2/schedules`から、月初〜月末を指定して取得します。

| パラメータ | 指定値 |
| --- | --- |
| `page_ctx_name` | `staff` |
| `store_id` | ログインした本人の所属店舗ID |
| `genre_ids[]` | 本人の所属業態ID |
| `start_date` / `end_date` | 対象月の最初と最後の日。`YYYY-MM-DD` |
| `is_staff_print_page` | `false` |

店舗IDと業態IDは、ブラウザのAPI入力から受け取りません。認証時の利用者情報を使います。認証引き継ぎページから取得できたCSRFトークンを、シフト取得時の`x-csrf-token`に付けます。

外部JSONの`users`をメンバー、`shared`をシフトへ変換します。`start_time`、`end_time`、`rest_times`を時刻モデルへ変換し、データ形式が不正な場合は`502`を返します。シフト表示と給与計算は`attending_store_id`が本人の所属店舗と一致するデータを対象にします。

## 実行環境ごとの処理

### ローカル版

`requests.Session`が利用者ごとにらくしふのCookieを保持します。認証とシフト取得には各リクエストで20秒のタイムアウトを指定しています。

`MemorySessionStore`は、トークンに紐づく接続・閲覧者・月別キャッシュをプロセスのメモリに置きます。期限切れの削除は、セッションの作成・参照時に実行します。セッション上限は500件で、超過時は期限が最も早いセッションを削除します。

同じセッションのシフト取得とキャッシュ更新はロックで保護します。複数のサーバープロセス間でセッションを共有する実装はありません。プロセスを終了すると認証情報・シフト・ログイン試行履歴が失われます。

### Workers版

`workers.wsgi.entrypoint(app)`がFlaskへの入口です。Python Workersの実行環境からJavaScriptの`fetch`を呼び出し、同期処理からは`pyodide.ffi.run_sync`で非同期通信やDurable Objectの操作を待ちます。

認証Cookieは`WorkerCookieJar`で受け取り、ドメインとパスに合う送信先へ付けます。通信先はHTTPSの次の3ホストに限定し、転送先にも同じ制限を適用します。

- `api.accounts.rakushifu.com`
- `accounts.rakushifu.com`
- `skylark.enterprise.rakushifu.com`

Workers版には、ローカル版の`timeout=20`に対応する明示的な通信タイマーは実装していません。リダイレクトは手動で処理し、試行ループの上限を9回に制限しています。

`SESSIONS.getByName(token)`で、アプリのセッションごとに`SessionObject`へアクセスします。ログイン時は利用者情報・Cookie・CSRFトークン・有効期限をストレージへ保存し、最初の当月シフトをObjectのメモリキャッシュへ渡します。

各APIリクエストでは保存済み状態から閲覧者と接続アダプターを復元します。月別データが必要になるとObjectの`month`を呼び、そこでキャッシュ判定と再取得を行います。Objectの再起動後も認証状態は復元できますが、メモリキャッシュは復元せず再取得します。

`LOGIN_LIMITS`は、IP単位とIP・従業員IDの組合せごとに別の`LoginLimitObject`を使います。試行履歴はストレージに保存し、Alarmで削除します。IPは`CF-Connecting-IP`を優先して取得します。

静的ファイルのリクエストもWorkerが受け、Flaskの`/static/<filename>`ルートから`ASSETS.fetch`へ渡します。テンプレートはWorkerに含め、CSSとJavaScriptは静的アセットとして配置します。

## 月別キャッシュと画面更新

| 項目 | 仕様 |
| --- | --- |
| キャッシュの単位 | アプリのセッションと年月 |
| 有効期間 | 取得後120秒 |
| 上限 | 1セッションにつき3か月 |
| 上限到達時 | 期限が最も早い月を除く |
| キャッシュ失効時 | 次の参照でらくしふから取得 |
| 保存先 | ローカルはセッションのメモリ、WorkersはObjectのメモリ |

期限切れの月を再取得するとき、期限切れのキャッシュを整理します。月別データをJSONファイルやDBへ永続保存する処理はありません。

Workers版では、APIリクエスト中のアプリケーション層にも一時キャッシュを作ります。同じリクエスト内で利用するためのもので、リクエストをまたぐキャッシュはDurable Objectが担当します。

ブラウザはカレンダーの応答後、`CACHE_SECONDS × 1000 + 1000`ミリ秒で再取得を予約します。既定では121秒です。失敗時は最大30秒後に再試行します。検索と給与の入力には250ミリ秒の待ち時間を設け、古いリクエストの結果で新しい表示を上書きしないよう管理します。

## 認証とデータの保持

トークンは`secrets.token_urlsafe(32)`で生成します。らくしふの認証Cookieをブラウザへ渡す用途には使いません。アプリCookieの名前は`app_session`、属性は`HttpOnly`・`SameSite=Lax`・`Path=/`、最大寿命は3600秒です。Workersでは`Secure`も必須です。

| データ | ローカル版 | Workers版 | 削除・失効 |
| --- | --- | --- | --- |
| パスワード | 認証リクエストで使用 | 同左 | 保存処理なし |
| 利用者識別情報・認証Cookie・CSRFトークン | プロセスメモリ | `SessionObject`ストレージ | ログアウト、期限切れ。ローカルではプロセス終了でも消失 |
| 月別シフト・メンバー | セッションのメモリ | Objectのメモリ | キャッシュ整理、セッション削除、プロセス・Objectのメモリ消失 |
| ログイン試行履歴 | プロセスメモリ | `LoginLimitObject`ストレージ | 直近300秒を集計。Workersでは最終記録から300秒後のAlarmで削除 |
| 時給・深夜割増率 | ブラウザの`localStorage` | 同左 | サイトデータ削除時。ログアウトでは残る |

セッションの期限は作成から1時間で、アクセスしても延長しません。Workersでは有効期限にAlarmを設定し、期限切れの状態を削除します。参照時にも期限を確認します。Alarmによる削除が有効期限と完全に同時に行われることを前提にはしていません。

ログアウトはアプリの保存状態とブラウザのアプリCookieを削除します。らくしふへセッション失効のリクエストを送る処理はありません。

Workersではシフト取得の通信後にもセッションの存在・有効期限を確認します。取得中にログアウトまたは期限切れとなった場合、取得結果を保存・返却しません。

`/api/*`と`/login`の応答には`Cache-Control: no-store`を付けます。これはHTTP応答を保存させない指定で、アプリ内の120秒キャッシュとは別です。シフトや給与入力はアプリのサーバーで処理するため、Workersで公開した場合はそれらの処理もCloudflare上で実行されます。

## HTTP API

APIはアプリCookieで認証します。ブラウザから別のトークンを発行する公開APIや、APIキー認証はありません。成功時の応答はJSONです。時刻は`23:00`・`25:00`のような文字列で表し、集計時間は分単位の整数で返します。

### 画面・認証ルート

| メソッド・パス | 入力 | 応答 |
| --- | --- | --- |
| `GET /` | なし | アプリ画面。未認証なら`/login`へ302 |
| `GET /login` | なし | ログイン画面。認証済みなら`/`へ302 |
| `POST /login` | JSONの`employee_code`、`password` | `{"message":"ログイン成功"}`とCookie |
| `POST /logout` | アプリCookie | セッションを削除し、`/login`へ302 |
| `GET /static/<filename>` | ファイル名 | CSS・JavaScriptなど |

従業員IDは入力時点で50文字以下とし、前後の空白を除いた結果が空ならエラーにします。パスワードは必須で500文字以下です。これらは利用者入力の検証条件であり、らくしふ側のパスワード規則を示すものではありません。

リクエスト本文の上限は8KiBです。上限を超えるとJSON形式のエラーをHTTP 413で返します。Workersの入口では、`Content-Length`がない場合も実際に読み込んだバイト数を制限し、Flaskへ渡す前に拒否します。Flaskにも同じ上限を設定しています。

### シフト・メンバー・給与

年と月は`year`と`month`で指定します。月は1〜12です。`date`は厳密な`YYYY-MM-DD`形式です。

| メソッド・パス | 入力 | 成功応答の主な項目 |
| --- | --- | --- |
| `GET /api/calendar` | クエリ：`year`、`month` | 日付をキーとした`has_me`、`my_shift_time`、`my_shift_end_time`、`total_count` |
| `GET /api/shifts` | クエリ：`date` | `date`、`count`、`has_my_shift`、`workers` |
| `GET /api/staff` | クエリ：`year`、`month`、任意の`q`（100文字以下） | メンバーの配列。`user_id`、`name`、`employee_code`、`rank`、`has_shift` |
| `GET /api/staff/<user_id>` | パス：整数のID。クエリ：`year`、`month` | メンバー情報、`schedules`、`total_work_minutes` |
| `POST /api/pay/estimate` | JSON：`year`、`month`、`hourly_wage`、`night_bonus_percent` | `user_id`、`shift_count`、`scheduled_minutes`、`break_minutes`、`worked_minutes`、`night_minutes`、`estimated_yen` |

カレンダーはシフトのある日だけを返します。`my_shift_end_time`は本人のシフトがある日に付けます。日別の`workers`には、氏名、年齢、ランク、勤務時刻、休憩、本人判定、時間重複判定、同時退勤判定を含みます。

メンバー詳細の`schedules`には、日付、曜日、勤務時刻、休憩時刻、`duration_minutes`、`rest_minutes`を含みます。この応答の`duration_minutes`は、シフトの拘束時間ではなく休憩控除後の実働時間です。

以下は架空の条件で給与概算を要求する例です。ブラウザはログイン時のCookieを自動で送信します。

```json
{
  "year": 2026,
  "month": 10,
  "hourly_wage": "1200",
  "night_bonus_percent": "25"
}
```

給与計算の丸め、休憩処理、日付跨ぎの扱いは[利用ガイド](usage.md#給与の計算方法)に記載しています。

### エラー応答

アプリが処理するAPIエラーは`{"error":"説明"}`形式です。存在しないURLや、フレームワークが処理するエラーがすべてこの形式になるわけではありません。

| ステータス | 条件 |
| --- | --- |
| `400` | 必須入力不足、日付・年月の不正、給与設定の範囲外など |
| `401` | 未認証、期限切れ、認証情報の不一致、らくしふの認証失効 |
| `404` | メンバーが存在しない、または表示対象範囲に含まれない |
| `429` | ログイン試行の上限に到達 |
| `502` | らくしふへの通信、認証引き継ぎ、応答形式のエラー |
| `503` | Durable Objectsの操作に失敗 |

Workersの必須バインディング欠落は構成エラーです。通常の保存先障害とは異なり、すべてを`503`へ変換する処理はありません。

## データモデルと表示範囲

| モデル | 主な値 |
| --- | --- |
| `TimeOfDay` | 時0〜47、分0〜59。翌日の時刻を24以上の時で表現できる |
| `Shift` | 日付、メンバーID、勤務店舗ID、開始・終了、複数の休憩 |
| `Staff` | ID、名前、年齢、ランク、誕生日、従業員コード、所属店舗ID |
| `ShiftMonth` | 年月、その月のメンバー一覧とシフト |
| `Viewer` | アカウントID、本人のスタッフID、所属店舗ID、所属業態ID |

カレンダー・日別一覧・給与は、本人の所属店舗と一致し、開始時刻があるシフトを使います。メンバー詳細は、本人、自店舗に所属する人、表示月に自店舗で勤務する人を参照できます。取得した利用者情報を、そのまま全社名簿として公開するAPIはありません。

## 用語

| 用語 | 意味 |
| --- | --- |
| Flask | 画面やHTTP APIを実装するPythonのWebフレームワーク |
| HTTP API | ブラウザなどからHTTPで呼び出し、データや処理結果を返す窓口 |
| JSON | APIの入出力や保存状態で使う、キーと値などを表すデータ形式 |
| セッション | ログインした利用者の認証状態を、複数のリクエストにわたって保持する単位 |
| Cookie | ブラウザがサイトごとに保存し、リクエスト時にサーバーへ送る値 |
| WSGI | FlaskなどのPython WebアプリをHTTP実行環境につなぐインターフェース |
| ポート | アプリケーションが外部通信や保存に求める操作の定義 |
| キャッシュ | 取得済みデータを一定時間再利用する仕組み |
| CSRFトークン | らくしふ側のリクエスト検証に使う値。このアプリのCookieとは別 |
| Alarm | Durable Objectの指定時刻以降に処理を実行する仕組み |

[READMEに戻る](../README.md)
