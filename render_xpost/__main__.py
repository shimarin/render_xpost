#!/usr/bin/env python3
import asyncio
import argparse
from pathlib import Path
import render_xpost

parser = argparse.ArgumentParser(description="X ポストを画像にレンダリング")
parser.add_argument("token_file", metavar="token_file", help="ベアラートークンファイルのパス")
parser.add_argument("tweet_id", metavar="tweet_id_or_url")
parser.add_argument("output", nargs="?", default=None)
parser.add_argument("--light", action="store_true", help="ライトモードで描画")
args = parser.parse_args()

render_xpost.set_bearer_token(Path(args.token_file).read_text().strip())

out = args.output or f"post_{args.tweet_id}.png"
data = asyncio.run(render_xpost.render_single_post(args.tweet_id, theme="light" if args.light else "dark"))
Path(out).write_bytes(data)
print(f"Saved: {out}")
