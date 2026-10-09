#!/usr/bin/env python3
"""議会活動データ取得の第1段階: 発言候補と公式会議録を保存する。
質問・答弁の対応を推測で作らず、公開前の確認用ファイルとして出力する。
"""
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
from playwright.async_api import async_playwright

PROFILE = "https://www.jichiscope.com/ward/koto/giin/26"
OUTPUT = Path("collected_records")
OUTPUT.mkdir(exist_ok=True)
OFFICIAL = OUTPUT / "official_text"
OFFICIAL.mkdir(exist_ok=True)
NAME = re.compile(r"^○\s*鈴木[\s　]*綾子\s*$")
SPEAKER = re.compile(r"^○\s*\S+")
END_MARKERS = ("このページの読み方", "自治スコープ ｜", "© 2026")

def extract_speeches(text):
    """可視テキストから本人の発言候補を取り出す。自動で答弁を割り当てない。"""
    if "発言の記録" not in text:
        return []
    text = text.split("発言の記録", 1)[1]
    lines = [s.strip() for s in text.splitlines()]
    result, capturing, parts = [], False, []
    for line in lines:
        if any(line.startswith(x) for x in END_MARKERS):
            break
        if SPEAKER.match(line):
            if capturing and parts:
                speech = "\n".join(parts).strip()
                if len(speech) > 10:
                    result.append(speech)
            capturing, parts = bool(NAME.match(line)), []
            continue
        if capturing:
            if line.startswith(("江東新時代の会", "続きを読む", "閉じる")):
                continue
            if line:
                parts.append(line)
    if capturing and parts:
        speech = "\n".join(parts).strip()
        if len(speech) > 10:
            result.append(speech)
    return result

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(locale="ja-JP", user_agent="CouncilRecordResearch/0.1 (public records; manual verification)")
        await page.goto(PROFILE, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1200)
        # 「さらに表示」を押して、過去の会議へのリンクも展開
        for _ in range(20):
            buttons = page.get_by_role("button", name=re.compile("さらに表示"))
            if not await buttons.count():
                break
            btn = buttons.first
            if not await btn.is_visible():
                break
            await btn.click()
            await page.wait_for_timeout(350)
        links = await page.locator('a[href*="/ward/koto/kaigiroku/"]').evaluate_all(
            "(els) => [...new Set(els.map(a => a.href.split('?')[0]))]"
        )
        links = [x for x in links if re.search(r"/kaigiroku/\d+$", x)]
        if not links:
            raise RuntimeError("会議録リンクを取得できません。サイト構造を確認してください。")
        records, meeting_reports = [], []
        for i, link in enumerate(links, 1):
            try:
                await page.goto(link, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(300)
                # 会議録の発言部分が折り畳まれている場合は開く
                toggles = page.get_by_text(re.compile("発言の記録.*開く"))
                if await toggles.count() and await toggles.first.is_visible():
                    await toggles.first.click()
                # 各発言の「続きを読む」を展開（長文の省略を避ける）
                for _ in range(150):
                    btn = page.get_by_role("button", name=re.compile("続きを読む"))
                    if not await btn.count():
                        break
                    try:
                        await btn.first.click(timeout=2000)
                    except Exception:
                        break
                body = await page.locator("body").inner_text()
                heading = await page.locator("h1").first.inner_text()
                official_links = await page.locator('a[href*="city.koto.tokyo.dbsr.jp"]').evaluate_all(
                    "(els) => [...new Set(els.map(a => a.href))]"
                )
                official_url = official_links[0] if official_links else None
                match = re.search(r"20\d{2}[.年/-]\d{1,2}[.月/-]\d{1,2}", body[:600])
                date = match.group(0) if match else ""
                speeches = extract_speeches(body)
                for j, speech in enumerate(speeches, 1):
                    records.append({
                        "id": f"candidate-{i:03d}-{j:03d}",
                        "date_display": date,
                        "meeting": heading,
                        "speaker": "鈴木綾子",
                        "speech_text_candidate": speech,
                        "secondary_source_url": link,
                        "official_source_url": official_url,
                        "question_answer_match": "未確認",
                        "publication_status": "要原文照合",
                    })
                official_status = "未取得"
                if official_url:
                    official_page = await browser.new_page(locale="ja-JP")
                    try:
                        await official_page.goto(official_url, wait_until="domcontentloaded", timeout=20000)
                        raw = await official_page.locator("body").inner_text(timeout=10000)
                        if len(raw) > 150:
                            (OFFICIAL / f"meeting_{i:03d}.txt").write_text(raw, encoding="utf-8")
                            official_status = "取得"
                        else:
                            official_status = "内容不足"
                    except Exception as exc:
                        official_status = f"取得失敗: {type(exc).__name__}"
                    finally:
                        await official_page.close()
                meeting_reports.append({
                    "meeting": heading, "secondary_url": link, "official_url": official_url,
                    "candidate_speeches": len(speeches), "official_text": official_status,
                })
                print(f"[{i}/{len(links)}] {heading}: {len(speeches)}発言候補 / 公式:{official_status}", flush=True)
                await page.wait_for_timeout(500)
            except Exception as exc:
                meeting_reports.append({"secondary_url": link, "error": str(exc)})
                print(f"[{i}/{len(links)}] ERROR {link}: {exc}", flush=True)
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "purpose": "公開前の発言候補データ。公式原文との照合が必要",
            "source_note": "会議一覧・候補発言は自治スコープを利用。公式会議録本文の取得結果はofficial_textフォルダを参照",
            "records": records,
        }
        (OUTPUT / "speech_candidates.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (OUTPUT / "report.json").write_text(json.dumps(meeting_reports, ensure_ascii=False, indent=2), encoding="utf-8")
        await browser.close()
        if not records:
            raise RuntimeError("発言候補を取得できませんでした。report.jsonを確認してください。")
        print(f"完了: {len(links)}会議 / {len(records)}発言候補。公開用サイトへの自動反映は行いません。", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
