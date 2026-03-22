"""
X(Twitter)のポストを画像にレンダリングするモジュール
"""

import sys
import asyncio
import math
import io
import html
import re
from html.parser import HTMLParser
from pathlib import Path
from datetime import datetime

import cairo
import gi
gi.require_version('Pango', '1.0')
gi.require_version('PangoCairo', '1.0')
from gi.repository import Pango, PangoCairo

import httpx

# --- 設定 ---
WIDTH = 600
PADDING = 24
AVATAR_SIZE = 48
QUOTE_AVATAR_SIZE = 32
FONT_FAMILY = "Sans"
MEDIA_RADIUS = 12
LINK_CARD_QR_SIZE = 100

THEMES = {
    "dark": dict(
        CARD_COLOR       = (0.13, 0.13, 0.13),
        QUOTE_BG_COLOR   = (0.19, 0.19, 0.19),
        QUOTE_BORDER_COLOR = (0.30, 0.30, 0.30),
        TEXT_COLOR       = (1.0,  1.0,  1.0 ),
        SUB_COLOR        = (0.55, 0.55, 0.55),
        ACCENT_COLOR     = (0.11, 0.63, 0.95),
        LINK_COLOR       = "#1BA1F1",
    ),
    "light": dict(
        CARD_COLOR       = (1.0,  1.0,  1.0 ),
        QUOTE_BG_COLOR   = (0.94, 0.94, 0.94),
        QUOTE_BORDER_COLOR = (0.80, 0.80, 0.80),
        TEXT_COLOR       = (0.05, 0.05, 0.05),
        SUB_COLOR        = (0.45, 0.45, 0.45),
        ACCENT_COLOR     = (0.11, 0.63, 0.95),
        LINK_COLOR       = "#1A8CD8",
    ),
}

# デフォルト（dark）。render_single_post() 呼び出し前に apply_theme() で上書き
CARD_COLOR        = THEMES["dark"]["CARD_COLOR"]
QUOTE_BG_COLOR    = THEMES["dark"]["QUOTE_BG_COLOR"]
QUOTE_BORDER_COLOR= THEMES["dark"]["QUOTE_BORDER_COLOR"]
TEXT_COLOR        = THEMES["dark"]["TEXT_COLOR"]
SUB_COLOR         = THEMES["dark"]["SUB_COLOR"]
ACCENT_COLOR      = THEMES["dark"]["ACCENT_COLOR"]
LINK_COLOR        = THEMES["dark"]["LINK_COLOR"]


def apply_theme(name: str):
    global CARD_COLOR, QUOTE_BG_COLOR, QUOTE_BORDER_COLOR
    global TEXT_COLOR, SUB_COLOR, ACCENT_COLOR, LINK_COLOR
    t = THEMES[name]
    CARD_COLOR         = t["CARD_COLOR"]
    QUOTE_BG_COLOR     = t["QUOTE_BG_COLOR"]
    QUOTE_BORDER_COLOR = t["QUOTE_BORDER_COLOR"]
    TEXT_COLOR         = t["TEXT_COLOR"]
    SUB_COLOR          = t["SUB_COLOR"]
    ACCENT_COLOR       = t["ACCENT_COLOR"]
    LINK_COLOR         = t["LINK_COLOR"]


_X_POST_URL_RE = re.compile(
    r'https?://(?:(?:www\.|mobile\.)?(?:twitter|x)\.com)/\w+/status/(\d+)'
)


def is_x_post_url(s: str) -> bool:
    return bool(_X_POST_URL_RE.search(s))


def _resolve_tweet_id(s: str) -> str:
    m = _X_POST_URL_RE.search(s)
    return m.group(1) if m else s


_bearer_token: str | None = None


def set_bearer_token(token: str):
    global _bearer_token
    _bearer_token = token


async def fetch_tweet(client: httpx.AsyncClient, tweet_id: str) -> dict:
    if _bearer_token is None:
        raise RuntimeError("Bearer token is not set. Call set_bearer_token() before rendering.")
    url = f"https://api.x.com/2/tweets/{tweet_id}"
    params = {
        "tweet.fields": "created_at,text,public_metrics,author_id,attachments,entities,referenced_tweets,note_tweet",
        "expansions": "author_id,attachments.media_keys,referenced_tweets.id,referenced_tweets.id.author_id",
        "user.fields": "name,username,profile_image_url",
        "media.fields": "type,url,preview_image_url,width,height,alt_text",
    }
    r = await client.get(url, headers={"Authorization": f"Bearer {_bearer_token}"}, params=params)
    r.raise_for_status()
    return r.json()


async def download_image(client: httpx.AsyncClient, url: str,
                         referer: str | None = None) -> cairo.ImageSurface | None:
    try:
        headers = {"Referer": referer} if referer else {}
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        data = r.content
        if data[:4] == b'\x89PNG':
            surf = cairo.ImageSurface.create_from_png(io.BytesIO(data))
        else:
            from PIL import Image
            img = Image.open(io.BytesIO(data)).convert("RGBA")
            buf = io.BytesIO()
            img.save(buf, "PNG")
            buf.seek(0)
            surf = cairo.ImageSurface.create_from_png(buf)
        return surf
    except Exception as e:
        print(f"[warn] image fetch failed ({url}): {e}", file=sys.stderr)
        return None


async def fetch_avatar(client: httpx.AsyncClient, url: str) -> cairo.ImageSurface | None:
    return await download_image(client, url.replace("_normal", "_bigger"))


class _OGPParser(HTMLParser):
    """og:image と記事公開日時を抽出する軽量パーサ"""
    _DATE_PROPS = {
        "article:published_time",
        "og:article:published_time",
        "datePublished",
        "article:modified_time",
    }

    def __init__(self):
        super().__init__()
        self.image_url: str | None = None
        self.article_date: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        d = dict(attrs)
        prop = d.get("property") or d.get("name") or ""
        content = d.get("content", "")
        if not self.image_url and prop == "og:image" and content:
            self.image_url = content
        if not self.article_date and prop in self._DATE_PROPS and content:
            self.article_date = content


async def fetch_ogp_data(
    client: httpx.AsyncClient, url: str
) -> tuple[cairo.ImageSurface | None, str | None]:
    """指定 URL のページから og:image と記事公開日を取得する。
    返値: (ImageSurface | None, article_date_str | None)
    失敗時は (None, None)。
    """
    try:
        r = await client.get(url)
        if "html" not in r.headers.get("content-type", ""):
            return None, None
        parser = _OGPParser()
        parser.feed(r.text)
        surf = None
        if parser.image_url:
            surf = await download_image(client, parser.image_url, referer=url)
        return surf, parser.article_date
    except Exception as e:
        print(f"[warn] OGP fetch failed ({url}): {e}", file=sys.stderr)
        return None, None


def _make_stub_surface(w: int, h: int) -> cairo.ImageSurface:
    """OGP画像取得失敗時のスタブ（ページアイコン風）を生成する"""
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surf)
    cr.set_source_rgb(*QUOTE_BG_COLOR)
    cr.paint()
    # ページアイコン: 中央に小さな矩形＋横線2本
    iw, ih = w * 0.35, h * 0.45
    ix, iy = (w - iw) / 2, (h - ih) / 2
    cr.set_source_rgb(*QUOTE_BORDER_COLOR)
    cr.set_line_width(1.5)
    draw_rounded_rect(cr, ix, iy, iw, ih, 2)
    cr.stroke()
    lx1, lx2 = ix + iw * 0.15, ix + iw * 0.85
    for frac in (0.38, 0.58, 0.75):
        ly = iy + ih * frac
        cr.move_to(lx1, ly)
        cr.line_to(lx2, ly)
        cr.stroke()
    return surf


def make_pango_layout(cr: cairo.Context, text: str, size_pt: float,
                      bold: bool = False, color=None, width_px: int = 0):
    layout = PangoCairo.create_layout(cr)
    desc = Pango.FontDescription.new()
    desc.set_family(FONT_FAMILY)
    desc.set_size(int(size_pt * Pango.SCALE))
    desc.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
    layout.set_font_description(desc)
    layout.set_text(text, -1)
    if width_px > 0:
        layout.set_width(width_px * Pango.SCALE)
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    cr.set_source_rgb(*(color if color is not None else TEXT_COLOR))
    return layout


def draw_rounded_rect(cr: cairo.Context, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi/2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi/2)
    cr.arc(x + r, y + h - r, r, math.pi/2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3*math.pi/2)
    cr.close_path()


def _top_rounded_rect(cr: cairo.Context, x, y, w, h, r):
    """上側2角だけ角丸にした矩形パス（下側は直角）"""
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi/2, 0)  # 右上
    cr.line_to(x + w, y + h)                      # 右下（直角）
    cr.line_to(x, y + h)                           # 左下（直角）
    cr.arc(x + r, y + r, r, math.pi, 3*math.pi/2) # 左上
    cr.close_path()


def clip_circle(cr: cairo.Context, cx, cy, r):
    cr.arc(cx, cy, r, 0, 2 * math.pi)
    cr.clip()


def format_date(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt.astimezone().strftime("%Y年%m月%d日 %H:%M")


def build_body_markup(text: str, url_entities: list, exclude_tco: set = None) -> str:
    """
    ツイートテキストをPangoマークアップに変換する。
    - media_key を持つURLエンティティ（メディア添付）はテキストから除去
    - exclude_tco に含まれるt.co URL（引用ツイートURLなど）も除去
    - 通常URLはdisplay_urlをリンク色・下線で表示
    """
    exclude_tco = exclude_tco or set()
    entities = sorted(url_entities, key=lambda e: e["start"])
    parts = []
    pos = 0
    for ent in entities:
        start, end = ent["start"], ent["end"]
        parts.append(html.escape(text[pos:start]))
        if "media_key" in ent or ent["url"] in exclude_tco:
            pass  # 除去
        else:
            display = ent.get("display_url") or ent.get("expanded_url") or ent["url"]
            parts.append(
                f'<span foreground="{LINK_COLOR}"><u>{html.escape(display)}</u></span>'
            )
        pos = end
    parts.append(html.escape(text[pos:]))
    return "".join(parts).strip()


def calc_media_layout(surfs: list, total_w: int):
    n = len(surfs)
    gap = 4
    cols = min(n, 2)
    cell_w = (total_w - gap * (cols - 1)) // cols
    rows = math.ceil(n / cols)
    row_h = []
    for r in range(rows):
        h = 0
        for c in range(cols):
            i = r * cols + c
            if i >= n:
                break
            sw, sh = surfs[i].get_width(), surfs[i].get_height()
            h = max(h, int(cell_w * sh / sw))
        row_h.append(h)
    layout = []
    for i, surf in enumerate(surfs):
        col = i % cols
        row = i // cols
        layout.append((col, row, cell_w, row_h[row]))
    return layout, gap, row_h


def draw_media_grid(cr: cairo.Context, surfs: list, x: int, y: int,
                    total_w: int, radius: int) -> int:
    layout, gap, row_h = calc_media_layout(surfs, total_w)
    row_y = [0]
    for h in row_h[:-1]:
        row_y.append(row_y[-1] + h + gap)
    for surf, (col, row, cell_w, cell_h) in zip(surfs, layout):
        dx = x + col * (cell_w + gap)
        dy = y + row_y[row]
        sw, sh = surf.get_width(), surf.get_height()
        scale = cell_w / sw
        oy = (cell_h - sh * scale) / 2
        cr.save()
        draw_rounded_rect(cr, dx, dy, cell_w, cell_h, radius)
        cr.clip()
        cr.translate(dx, dy + oy)
        cr.scale(scale, scale)
        cr.set_source_surface(surf, 0, 0)
        cr.paint()
        cr.restore()
    return sum(row_h) + gap * (len(row_h) - 1)


# ---------- 引用ツイートカード ----------

def measure_quote_card(cr: cairo.Context, q_tweet: dict, q_user: dict,
                        card_w: int) -> int:
    """引用カードの高さを計算して返す（描画はしない）"""
    p = PADDING // 2
    inner_w = card_w - p * 2
    # ヘッダ行: アバター + 名前 + スクリーン名（1行）
    header_h = QUOTE_AVATAR_SIZE
    # 本文
    url_ents = q_tweet.get("entities", {}).get("urls", [])
    markup = build_body_markup(q_tweet["text"], url_ents)
    layout = make_pango_layout(cr, "", 13, width_px=inner_w)
    layout.set_markup(markup, -1)
    _, body_h = layout.get_pixel_size()
    # 日時
    date_layout = make_pango_layout(cr, format_date(q_tweet["created_at"]), 10, color=SUB_COLOR)
    _, date_h = date_layout.get_pixel_size()
    return p + header_h + 6 + body_h + 6 + date_h + p


def draw_quote_card(cr: cairo.Context, q_tweet: dict, q_user: dict,
                    q_avatar_surf: cairo.ImageSurface | None,
                    x: int, y: int, card_w: int) -> int:
    """引用カードを描画し、使った高さを返す"""
    p = PADDING // 2
    inner_w = card_w - p * 2
    card_h = measure_quote_card(cr, q_tweet, q_user, card_w)

    # 背景・枠線
    draw_rounded_rect(cr, x, y, card_w, card_h, 10)
    cr.set_source_rgb(*QUOTE_BG_COLOR)
    cr.fill_preserve()
    cr.set_source_rgb(*QUOTE_BORDER_COLOR)
    cr.set_line_width(1.0)
    cr.stroke()

    cy = y + p

    # アバター
    cr.save()
    clip_circle(cr, x + p + QUOTE_AVATAR_SIZE // 2,
                cy + QUOTE_AVATAR_SIZE // 2, QUOTE_AVATAR_SIZE // 2)
    if q_avatar_surf:
        scale = QUOTE_AVATAR_SIZE / max(q_avatar_surf.get_width(), q_avatar_surf.get_height())
        cr.translate(x + p, cy)
        cr.scale(scale, scale)
        cr.set_source_surface(q_avatar_surf, 0, 0)
        cr.paint()
    else:
        cr.set_source_rgb(*ACCENT_COLOR)
        cr.paint()
    cr.restore()

    # 名前 + @username（アバター右横、1行に並べる）
    name_x = x + p + QUOTE_AVATAR_SIZE + 8
    name_layout = make_pango_layout(cr, q_user["name"], 12, bold=True)
    cr.move_to(name_x, cy + (QUOTE_AVATAR_SIZE - 14) // 2)
    PangoCairo.show_layout(cr, name_layout)
    nw, _ = name_layout.get_pixel_size()

    sn_layout = make_pango_layout(cr, f"  @{q_user['username']}", 11, color=SUB_COLOR)
    cr.move_to(name_x + nw, cy + (QUOTE_AVATAR_SIZE - 13) // 2)
    PangoCairo.show_layout(cr, sn_layout)

    cy += QUOTE_AVATAR_SIZE + 6

    # 本文
    url_ents = q_tweet.get("entities", {}).get("urls", [])
    markup = build_body_markup(q_tweet["text"], url_ents)
    body_layout = make_pango_layout(cr, "", 13, width_px=inner_w)
    body_layout.set_markup(markup, -1)
    cr.set_source_rgb(*TEXT_COLOR)
    cr.move_to(x + p, cy)
    PangoCairo.show_layout(cr, body_layout)
    _, bh = body_layout.get_pixel_size()
    cy += bh + 6

    # 日時
    date_layout = make_pango_layout(cr, format_date(q_tweet["created_at"]), 10, color=SUB_COLOR)
    cr.move_to(x + p, cy)
    PangoCairo.show_layout(cr, date_layout)

    return card_h


# ---------- リンクカード ----------

def _link_card_img_h(card_w: int, ogp_surf: cairo.ImageSurface | None) -> int:
    """OGP画像のアスペクト比に合わせた表示高さを返す（最大16:9）"""
    max_h = card_w * 9 // 16
    if ogp_surf:
        sw, sh = ogp_surf.get_width(), ogp_surf.get_height()
        return min(int(card_w * sh / sw), max_h)
    return max_h


def _make_qr_surface(url: str, size: int) -> cairo.ImageSurface:
    import qrcode
    from PIL import Image as PilImage
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10, border=1,
    )
    qr.add_data(url)
    qr.make(fit=True)
    pil_img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
    pil_img = pil_img.resize((size, size), PilImage.NEAREST)
    buf = io.BytesIO()
    pil_img.save(buf, "PNG")
    buf.seek(0)
    return cairo.ImageSurface.create_from_png(buf)


def _format_article_date(iso: str) -> str:
    """記事公開日を「YYYY年MM月DD日」形式に変換する"""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt.astimezone().strftime("%Y年%m月%d日")


def measure_link_card(cr: cairo.Context, url_entity: dict,
                      ogp_surf: cairo.ImageSurface | None, card_w: int,
                      article_date: str | None = None) -> int:
    """リンクカードの高さを計算して返す（描画はしない）"""
    p = PADDING // 2
    img_h = _link_card_img_h(card_w, ogp_surf)
    desc = url_entity.get("description", "")
    date_str = _format_article_date(article_date) if article_date else None
    if not desc and not date_str:
        return img_h
    text_h = p  # 上パディング
    if desc:
        dl = make_pango_layout(cr, desc, 11, color=SUB_COLOR, width_px=card_w - p * 2)
        dl.set_height(-3)
        dl.set_ellipsize(Pango.EllipsizeMode.END)
        _, dh = dl.get_pixel_size()
        text_h += dh
    if date_str:
        dal = make_pango_layout(cr, date_str, 10, color=SUB_COLOR)
        _, dah = dal.get_pixel_size()
        text_h += (4 if desc else 0) + dah
    text_h += p  # 下パディング
    return img_h + text_h


def draw_link_card(cr: cairo.Context, url_entity: dict,
                   ogp_surf: cairo.ImageSurface | None,
                   x: int, y: int, card_w: int,
                   article_date: str | None = None) -> int:
    """リンクカードを描画し、使った高さを返す"""
    p = PADDING // 2
    img_h = _link_card_img_h(card_w, ogp_surf)
    total_h = measure_link_card(cr, url_entity, ogp_surf, card_w, article_date)
    has_text_area = total_h > img_h

    # --- 全体背景（text areaがある場合）---
    # カード全体を QUOTE_BG_COLOR で塗りつぶしてから画像を重ねる
    if has_text_area:
        draw_rounded_rect(cr, x, y, card_w, total_h, 10)
        cr.set_source_rgb(*QUOTE_BG_COLOR)
        cr.fill()

    # --- 画像（text areaがあれば上角丸のみ、なければ全角丸でクリップ）---
    cr.save()
    if has_text_area:
        _top_rounded_rect(cr, x, y, card_w, img_h, 10)
    else:
        draw_rounded_rect(cr, x, y, card_w, img_h, 10)
    cr.clip()

    img = ogp_surf or _make_stub_surface(card_w, img_h)
    sw, sh = img.get_width(), img.get_height()
    scale = max(card_w / sw, img_h / sh)
    ox = (card_w - sw * scale) / 2
    oy = (img_h - sh * scale) / 2
    cr.translate(x + ox, y + oy)
    cr.scale(scale, scale)
    cr.set_source_surface(img, 0, 0)
    cr.paint()
    cr.restore()

    # --- グラデーションオーバーレイ（画像エリア内）---
    cr.save()
    if has_text_area:
        _top_rounded_rect(cr, x, y, card_w, img_h, 10)
    else:
        draw_rounded_rect(cr, x, y, card_w, img_h, 10)
    cr.clip()

    grad_top = cairo.LinearGradient(0, y, 0, y + 36)
    grad_top.add_color_stop_rgba(0, 0, 0, 0, 0.55)
    grad_top.add_color_stop_rgba(1, 0, 0, 0, 0)
    cr.set_source(grad_top)
    cr.rectangle(x, y, card_w, 36)
    cr.fill()

    grad_bot = cairo.LinearGradient(0, y + img_h // 2, 0, y + img_h)
    grad_bot.add_color_stop_rgba(0, 0, 0, 0, 0)
    grad_bot.add_color_stop_rgba(1, 0, 0, 0, 0.70)
    cr.set_source(grad_bot)
    cr.rectangle(x, y + img_h // 2, card_w, img_h // 2)
    cr.fill()

    cr.restore()

    # --- テキスト・QRオーバーレイ（画像上）---

    # ドメイン（左上）
    domain = url_entity.get("display_url", "").split("/")[0]
    domain_l = make_pango_layout(cr, domain, 10, color=(1.0, 1.0, 1.0))
    cr.move_to(x + p, y + p)
    PangoCairo.show_layout(cr, domain_l)

    # QRコード（右下）- t.co短縮URLでシンプルに
    qr_url = url_entity.get("url", "")
    qr_surf = _make_qr_surface(qr_url, LINK_CARD_QR_SIZE)
    qr_x = x + card_w - p - LINK_CARD_QR_SIZE
    qr_y = y + img_h - p - LINK_CARD_QR_SIZE
    cr.set_source_surface(qr_surf, qr_x, qr_y)
    cr.paint()

    # タイトル（左下、QRと縦センタリング）
    title_w = card_w - LINK_CARD_QR_SIZE - p * 3
    title_l = make_pango_layout(cr, url_entity.get("title", ""), 13,
                                bold=True, color=(1.0, 1.0, 1.0), width_px=title_w)
    title_l.set_height(-2)
    title_l.set_ellipsize(Pango.EllipsizeMode.END)
    _, title_h = title_l.get_pixel_size()
    cr.move_to(x + p, qr_y + (LINK_CARD_QR_SIZE - title_h) // 2)
    PangoCairo.show_layout(cr, title_l)

    # --- 枠線（全体の角丸矩形）---
    draw_rounded_rect(cr, x, y, card_w, total_h, 10)
    cr.set_source_rgb(*QUOTE_BORDER_COLOR)
    cr.set_line_width(1.0)
    cr.stroke()

    # --- description・日付（背景は全体fill済みのため追加塗りつぶし不要）---
    desc = url_entity.get("description", "")
    below_y = y + img_h + p
    if desc:
        desc_l = make_pango_layout(cr, desc, 11, color=SUB_COLOR, width_px=card_w - p * 2)
        desc_l.set_height(-3)
        desc_l.set_ellipsize(Pango.EllipsizeMode.END)
        cr.move_to(x + p, below_y)
        PangoCairo.show_layout(cr, desc_l)
        _, desc_h = desc_l.get_pixel_size()
        below_y += desc_h
    if article_date:
        date_str = _format_article_date(article_date)
        date_l = make_pango_layout(cr, date_str, 10, color=SUB_COLOR)
        cr.move_to(x + p, below_y + (4 if desc else 0))
        PangoCairo.show_layout(cr, date_l)

    return total_h


# ---------- メイン描画 ----------

def draw_avatar(cr: cairo.Context, surf: cairo.ImageSurface | None,
                ax: int, ay: int, size: int):
    cr.save()
    clip_circle(cr, ax + size // 2, ay + size // 2, size // 2)
    if surf:
        scale = size / max(surf.get_width(), surf.get_height())
        cr.translate(ax, ay)
        cr.scale(scale, scale)
        cr.set_source_surface(surf, 0, 0)
        cr.paint()
    else:
        cr.set_source_rgb(*ACCENT_COLOR)
        cr.paint()
    cr.restore()


def _effective_text_and_urls(tweet: dict) -> tuple[str, list]:
    """note_tweet (長文ツイート) がある場合はそちらのテキストと URL エンティティを返す。
    通常ツイートでは text / entities.urls をそのまま返す。
    """
    if "note_tweet" in tweet:
        nt = tweet["note_tweet"]
        return nt.get("text", tweet["text"]), nt.get("entities", {}).get("urls", [])
    return tweet["text"], tweet.get("entities", {}).get("urls", [])


def _find_card_entity(tweet: dict, exclude_tco: set) -> dict | None:
    """tweet の entities.urls から リンクカード対象エントリを返す。なければ None。
    通常ツイートでは API が付与する title の有無でカードを判定する。
    Note Tweet では title が付与されないため、外部 URL をそのままカード候補とする。
    """
    is_note = "note_tweet" in tweet
    _, url_entities = _effective_text_and_urls(tweet)
    for ent in url_entities:
        if "media_key" in ent or ent["url"] in exclude_tco:
            continue
        if ent.get("title") or is_note:
            return ent
    return None


async def render_single_post(tweet_id: str, theme: str = "dark") -> bytes:
    apply_theme(theme)
    tweet_id = _resolve_tweet_id(tweet_id)

    _browser_ua = ("Mozilla/5.0 (X11; Linux x86_64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36")
    async with httpx.AsyncClient(
        headers={"User-Agent": _browser_ua},
        follow_redirects=True,
        timeout=30,
    ) as client:
        # --- データ取得 ---
        data = await fetch_tweet(client, tweet_id)
        tweet = data["data"]
        includes = data.get("includes", {})

        # ユーザーマップ（author_id → user）
        user_map = {u["id"]: u for u in includes.get("users", [])}
        user = user_map[tweet["author_id"]]

        # メディア情報（photoのみ）
        media_map = {
            m["media_key"]: m
            for m in includes.get("media", [])
            if m["type"] == "photo"
        }
        media_keys = tweet.get("attachments", {}).get("media_keys", [])
        photo_keys = [k for k in media_keys if k in media_map]
        photos = [media_map[k] for k in photo_keys]

        # 引用ツイートを特定
        q_tweet = None
        q_user = None
        quote_tco_urls = set()
        for ref in tweet.get("referenced_tweets", []):
            if ref["type"] == "quoted":
                qid = ref["id"]
                q_tweets = {t["id"]: t for t in includes.get("tweets", [])}
                if qid in q_tweets:
                    q_tweet = q_tweets[qid]
                    q_user = user_map.get(q_tweet.get("author_id", ""))
                # 引用ツイートに対応するt.co URLを除去対象に
                _, _all_urls = _effective_text_and_urls(tweet)
                for ent in _all_urls:
                    exp = ent.get("expanded_url", "")
                    if qid in exp and ("twitter.com" in exp or "x.com" in exp):
                        quote_tco_urls.add(ent["url"])
                break

        # リンクカードエンティティを特定（title を持つ最初の外部URL）
        card_ent = _find_card_entity(tweet, quote_tco_urls)

        # リンクカードのt.co URLも本文から除去する
        exclude_tco = set(quote_tco_urls)
        if card_ent:
            exclude_tco.add(card_ent["url"])

        # --- 画像を並列ダウンロード ---
        download_tasks = [fetch_avatar(client,user.get("profile_image_url", ""))]
        download_tasks += [download_image(client, p["url"]) for p in photos]
        if q_user:
            download_tasks.append(fetch_avatar(client,q_user.get("profile_image_url", "")))
        if card_ent:
            ogp_url = card_ent.get("unwound_url") or card_ent.get("expanded_url")
            download_tasks.append(fetch_ogp_data(client, ogp_url))

        results = await asyncio.gather(*download_tasks)

        idx = 0
        avatar_surf = results[idx]; idx += 1
        photo_surfs = [s for s in results[idx:idx + len(photos)] if s]; idx += len(photos)
        q_avatar_surf = results[idx] if q_user else None
        if q_user: idx += 1
        ogp_surf, article_date = results[idx] if card_ent else (None, None)

    # 本文マークアップ（引用URL・メディアURL・カードURL除去、通常URLはリンク表示）
    tweet_text, url_entities = _effective_text_and_urls(tweet)
    body_markup = build_body_markup(tweet_text, url_entities, exclude_tco=exclude_tco)

    metrics = tweet.get("public_metrics", {})
    created = format_date(tweet.get("created_at", ""))
    name = user["name"]
    screen_name = f"@{user['username']}"

    # --- レイアウト計算 ---
    text_width = WIDTH - PADDING * 2
    tmp_surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, WIDTH, 100)
    tmp_cr = cairo.Context(tmp_surf)

    body_layout = make_pango_layout(tmp_cr, "", 15, width_px=text_width)
    body_layout.set_markup(body_markup, -1)
    _, body_h = body_layout.get_pixel_size()

    header_h = AVATAR_SIZE + PADDING
    body_top = PADDING + header_h

    media_h = 0
    if photo_surfs:
        _, _, row_h = calc_media_layout(photo_surfs, text_width)
        media_h = sum(row_h) + 4 * (len(row_h) - 1) + PADDING

    quote_h = 0
    if q_tweet and q_user:
        quote_h = measure_quote_card(tmp_cr, q_tweet, q_user, text_width) + PADDING

    link_card_h = 0
    if card_ent:
        link_card_h = measure_link_card(tmp_cr, card_ent, ogp_surf, text_width, article_date) + PADDING

    metrics_h = 28
    footer_h = metrics_h + PADDING * 2
    total_h = body_top + body_h + PADDING + media_h + quote_h + link_card_h + footer_h + PADDING

    # --- 本描画 ---
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, WIDTH, total_h)
    cr = cairo.Context(surf)

    draw_rounded_rect(cr, PADDING//2, PADDING//2,
                      WIDTH - PADDING, total_h - PADDING, 16)
    cr.set_source_rgb(*CARD_COLOR)
    cr.fill()

    # アバター
    draw_avatar(cr, avatar_surf, PADDING, PADDING, AVATAR_SIZE)

    # 名前 / スクリーン名
    name_x = PADDING + AVATAR_SIZE + 12
    name_y = PADDING + 4
    layout = make_pango_layout(cr, name, 14, bold=True)
    cr.move_to(name_x, name_y)
    PangoCairo.show_layout(cr, layout)
    _, name_h = layout.get_pixel_size()

    layout2 = make_pango_layout(cr, screen_name, 12, color=SUB_COLOR)
    cr.move_to(name_x, name_y + name_h + 2)
    PangoCairo.show_layout(cr, layout2)

    # X ロゴ
    logo_layout = make_pango_layout(cr, "\U0001D54F", 20, color=ACCENT_COLOR)
    logo_layout.set_font_description(Pango.FontDescription("STIX Two Math 20"))
    cr.move_to(WIDTH - PADDING - 24, PADDING + 4)
    PangoCairo.show_layout(cr, logo_layout)

    # 本文
    body_layout = make_pango_layout(cr, "", 15, width_px=text_width)
    body_layout.set_markup(body_markup, -1)
    cr.set_source_rgb(*TEXT_COLOR)
    cr.move_to(PADDING, body_top)
    PangoCairo.show_layout(cr, body_layout)

    cur_y = body_top + body_h + PADDING

    # メディア
    if photo_surfs:
        draw_media_grid(cr, photo_surfs, PADDING, cur_y, text_width, MEDIA_RADIUS)
        cur_y += media_h

    # 引用カード
    if q_tweet and q_user:
        draw_quote_card(cr, q_tweet, q_user, q_avatar_surf,
                        PADDING, cur_y, text_width)
        cur_y += quote_h

    # リンクカード
    if card_ent:
        draw_link_card(cr, card_ent, ogp_surf, PADDING, cur_y, text_width, article_date)
        cur_y += link_card_h

    # 日時
    layout3 = make_pango_layout(cr, created, 11, color=SUB_COLOR)
    cr.move_to(PADDING, cur_y)
    PangoCairo.show_layout(cr, layout3)
    _, date_h = layout3.get_pixel_size()

    # セパレータ
    sep_y = cur_y + date_h + 8
    cr.set_source_rgb(*SUB_COLOR)
    cr.set_line_width(0.5)
    cr.move_to(PADDING, sep_y)
    cr.line_to(WIDTH - PADDING, sep_y)
    cr.stroke()

    # メトリクス
    metrics_y = sep_y + 10
    mx = PADDING
    for item in [
        f"♥  {metrics.get('like_count', 0):,}",
        f"🔁  {metrics.get('retweet_count', 0):,}",
        f"💬  {metrics.get('reply_count', 0):,}",
    ]:
        layout_m = make_pango_layout(cr, item, 12, color=SUB_COLOR)
        cr.move_to(mx, metrics_y)
        PangoCairo.show_layout(cr, layout_m)
        w, _ = layout_m.get_pixel_size()
        mx += w + 32

    buf = io.BytesIO()
    surf.write_to_png(buf)
    return buf.getvalue()


async def render_auto(tweet_id: str, theme: str = "dark") -> bytes:
    """リンクカードがあればカード単独を、なければポスト全体をレンダリングする。
    カードあり: API 1回 / カードなし: API 2回（1回目の判定 + 2回目のフル描画）。
    """
    try:
        return await render_link_card(tweet_id, theme)
    except ValueError:
        return await render_single_post(tweet_id, theme)


async def render_link_card(tweet_id: str, theme: str = "dark") -> bytes:
    """ポストに含まれるリンクカードのみを描画して PNG バイト列を返す。
    リンクカードが存在しない場合は ValueError を送出する。
    """
    apply_theme(theme)
    tweet_id = _resolve_tweet_id(tweet_id)

    _browser_ua = ("Mozilla/5.0 (X11; Linux x86_64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36")
    async with httpx.AsyncClient(
        headers={"User-Agent": _browser_ua},
        follow_redirects=True,
        timeout=30,
    ) as client:
        data = await fetch_tweet(client, tweet_id)
        tweet = data["data"]

        card_ent = _find_card_entity(tweet, set())
        if card_ent is None:
            raise ValueError(f"No link card found in tweet {tweet_id}")

        ogp_url = card_ent.get("unwound_url") or card_ent.get("expanded_url")
        ogp_surf, article_date = await fetch_ogp_data(client, ogp_url)

    p = PADDING // 2
    text_width = WIDTH - PADDING * 2
    tmp_surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, WIDTH, 100)
    tmp_cr = cairo.Context(tmp_surf)
    card_h = measure_link_card(tmp_cr, card_ent, ogp_surf, text_width, article_date)
    total_h = p + card_h + p

    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, WIDTH, total_h)
    cr = cairo.Context(surf)
    draw_link_card(cr, card_ent, ogp_surf, PADDING, p, text_width, article_date)

    buf = io.BytesIO()
    surf.write_to_png(buf)
    return buf.getvalue()
