# render_xpost

X (旧Twitter) のポストを PNG 画像にレンダリングするパッケージ。
cairo + Pango でテキスト・画像・引用カードを描画し、X API v2 でデータを取得する。

## ファイル構成

```
render_xpost/
├── __init__.py    # ライブラリ本体
└── __main__.py    # CLI エントリポイント
```

## 使い方

### モジュールとして使う

```python
import asyncio
import render_xpost

render_xpost.set_bearer_token("your_bearer_token_here")

# ツイートID・URLのどちらでも渡せる
png_bytes = asyncio.run(render_xpost.render_single_post("1234567890", theme="dark"))
png_bytes = asyncio.run(render_xpost.render_single_post("https://x.com/user/status/1234567890", theme="dark"))

with open("output.png", "wb") as f:
    f.write(png_bytes)
```

`set_bearer_token()` を呼ばずに `render_single_post()` を実行すると `RuntimeError` が発生する。

URL かどうかの判定には `is_x_post_url()` を使える:

```python
render_xpost.is_x_post_url("https://x.com/user/status/1234567890")  # True
render_xpost.is_x_post_url("1234567890")                             # False
```

### CLI として使う

```
python -m render_xpost <token_file> <tweet_id_or_url> [output.png] [--light]
```

- `token_file`: ベアラートークンが書かれたファイルのパス
- `tweet_id_or_url`: ツイートID または ポストURL
- `output`: 省略時は `post_<tweet_id>.png`
- `--light`: ライトモードで描画（デフォルトはダーク）

## 依存ライブラリ

| ライブラリ | 用途 | 入手方法 |
|---|---|---|
| pycairo | 画像描画 | emerge (`dev-python/pycairo`) |
| pygobject / Pango / PangoCairo | テキストレイアウト | emerge (`dev-python/pygobject`) |
| aiohttp | 非同期HTTP通信 | emerge (`dev-python/aiohttp`) |
| Pillow | アバター等のJPEG→PNG変換 | emerge (`dev-python/pillow`) |
| STIX Two Math フォント | 𝕏 ロゴ文字 (U+1D54F) | emerge (`media-fonts/stix-fonts`) |

## X API v2 の使い方メモ

### エンドポイント

- ユーザー情報: `GET /2/users/by/username/{username}`
- ツイート取得: `GET /2/users/{id}/tweets`
- ツイート詳細: `GET /2/tweets/{id}`

### 1リクエストで必要なデータを全部取る

`expansions` パラメータを使うと関連データを1回のリクエストで取得できる。
このパッケージでは以下を指定している:

```python
params = {
    "tweet.fields": "created_at,text,public_metrics,author_id,attachments,entities,referenced_tweets",
    "expansions": "author_id,attachments.media_keys,referenced_tweets.id,referenced_tweets.id.author_id",
    "user.fields": "name,username,profile_image_url",
    "media.fields": "type,url,preview_image_url,width,height,alt_text",
}
```

展開されたデータは `response["includes"]` 以下に入る:
- `includes.users[]` — ツイート著者・引用元著者のユーザー情報
- `includes.media[]` — 添付メディア情報
- `includes.tweets[]` — 引用ツイートのツイート情報

### entities.urls の構造と URL の種別判別

`entities.urls[]` の各エントリは以下のフィールドを持つ:

```json
{
  "start": 32, "end": 55,
  "url": "https://t.co/xxxx",        // 本文中の短縮URL
  "expanded_url": "https://...",     // 展開先
  "display_url": "example.com/...",  // 表示用短縮形
  "media_key": "3_xxxx"             // 添付メディアURLのみ存在
}
```

種別の判定方法:
- **添付メディアURL**: `"media_key"` フィールドが存在する
- **引用ツイートURL**: `expanded_url` に引用先のツイートIDと `twitter.com` or `x.com` が含まれる
- **外部リンク**: 上記以外

添付メディア・引用ツイートのURLはテキスト末尾に自動付与されるので、表示時は除去する必要がある。

### アバター画像のサイズ

`profile_image_url` に含まれる `_normal` サフィックスは 48×48px。
`_bigger` に置換すると 73×73px が取得できる。

## 描画の仕組み

### レイアウト計算の流れ

cairo は事前に canvas サイズを決める必要があるため、以下の順で計算している:

1. ダミーの `ImageSurface` + `Context` を作成
2. Pango でテキストを流し込んで `get_pixel_size()` で高さを取得
3. メディアグリッド・引用カードの高さも計算
4. 合計を `total_h` として本番の `ImageSurface` を確保
5. 再度同じ内容を描画

### 画像の並列ダウンロード

`render_single_post()` 内で `asyncio.gather()` を使い、アバター・添付写真・引用アバターを並列取得している:

```python
download_tasks = [fetch_avatar(session, user["profile_image_url"])]
download_tasks += [download_image(session, p["url"]) for p in photos]
if q_user:
    download_tasks.append(fetch_avatar(session, q_user["profile_image_url"]))

results = await asyncio.gather(*download_tasks)
```

### Pango でのマークアップ（リンク色付け）

通常テキストは `layout.set_text()`、リンクを含む場合は `layout.set_markup()` を使う。
マークアップ内では HTML エスケープが必要 (`html.escape()`)。

```python
markup = f'通常テキスト <span foreground="#1BA1F1"><u>link text</u></span>'
layout.set_markup(markup, -1)
```

### make_pango_layout の color 引数に注意

```python
# NG: デフォルト引数はモジュールロード時に評価される
def make_pango_layout(cr, text, size, color=TEXT_COLOR): ...

# OK: None をデフォルトにして関数内で参照する
def make_pango_layout(cr, text, size, color=None):
    cr.set_source_rgb(*(color if color is not None else TEXT_COLOR))
```

テーマ切り替えのようにグローバル変数を実行時に書き換える場合、
デフォルト引数に直接グローバル変数を使うと切り替え前の値が固定されてしまう。

### 𝕏 ロゴ文字 (U+1D54F) の描画

Sans 等の一般フォントはこの文字を持たない。`STIX Two Math` フォントを明示的に指定する:

```python
logo_layout = make_pango_layout(cr, "\U0001D54F", 20)
logo_layout.set_font_description(Pango.FontDescription("STIX Two Math 20"))
```

### 透過 PNG

`cairo.FORMAT_ARGB32` で作成した `ImageSurface` はデフォルトで全画素が透明。
カード外の背景を塗りつぶさなければそのまま透過 PNG になる。

### 画像の描画（クリッピング）

円形クリップやラウンドレクトクリップは `cr.save()` / `cr.restore()` で囲む。
`cr.clip()` の後に `cr.scale()` → `cr.set_source_surface()` → `cr.paint()` の順で描画する。

## 今後の課題・拡張候補

- **動画サムネイル対応**: `type == "video"` のメディアは `preview_image_url` でサムネを取得できる
- **OGP カード対応**: `entities.urls[].images` に OGP 画像が入っている場合がある（YouTubeリンク等）
- **複数画像（3〜4枚）グリッド**: 現状は最大2列、3枚以上は未テスト
- **引用カード内メディア**: 引用ツイートに添付画像があっても現状は表示しない
- **フォント設定の外部化**: 日本語フォント・サイズ等をコンフィグで変更できるとよい
