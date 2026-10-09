"""スノーピーク キャンプ場の空き監視。

予約ページの「この客室の空室状況」カレンダーを読み、対象日が
「空室なし → 空室あり」に変わったときだけ通知する。
ログイン・予約操作は一切しない（表示を読むだけ）。

通知先:
- 環境変数 NTFY_TOPIC があれば、スマホの ntfy アプリへプッシュ通知
- Mac 上で動かしているときは macOS の通知とダイアログ
"""

import json
import os
import subprocess
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

# 監視するサイト。増やすときはここに1行足す（url は客室ページのURL）
SITES = [
    {
        "name": "鹿沼 電源付区画サイトH",
        "url": "https://go-snowpeak.reservation.jp/ja/hotels/sp-kanuma/rooms/10041205?dateUndecidedFlg=1",
    },
    {
        "name": "鹿沼 電源付区画サイトE",
        "url": "https://go-snowpeak.reservation.jp/ja/hotels/sp-kanuma/rooms/10041204?dateUndecidedFlg=1",
    },
    {
        "name": "白河 電源付サイト",
        "url": "https://go-snowpeak.reservation.jp/ja/hotels/sp-nasushirakawa/rooms/10030735?dateUndecidedFlg=1",
    },
]
TARGET_DATES = [date(2026, 11, 22), date(2026, 11, 28)]
STOP_AFTER = date(2026, 11, 28)  # この日を過ぎたら何もしない
FAILURE_NOTIFY_AT = 3  # 連続でこの回数失敗したら一度だけ知らせる

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
ON_MAC = sys.platform == "darwin"

# カレンダー1か月分を {日: 状態} にして返す（ページ内で実行するJavaScript）
READ_MONTH_JS = """
(label) => {
  const blocks = [...document.querySelectorAll('#room-search .c-calendarBox-daylist')];
  const block = blocks.find(b =>
    b.querySelector('.c-calendarBox-daylist-month').textContent.replace(/\\s+/g, ' ').trim() === label);
  if (!block) return null;
  const result = {};
  for (const li of block.querySelectorAll('ol > li')) {
    const dt = li.querySelector('dt');
    if (!dt) continue;
    const icon = li.querySelector('dd i');
    const remaining = li.querySelector('dd strong');
    let status;
    if (li.querySelector('a')) status = remaining ? 'available:' + remaining.textContent.trim() : 'available';
    else if (icon && icon.classList.contains('clear')) status = 'full';
    else if (icon && icon.classList.contains('c-icon-empty')) status = 'not_on_sale';
    else status = 'unknown';
    result[dt.textContent.trim()] = status;
  }
  return result;
}
"""


def log(message):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}", flush=True)


def notify(title, message, url=None):
    if NTFY_TOPIC:
        body = {"topic": NTFY_TOPIC, "title": title, "message": message, "priority": 5}
        if url:
            body["click"] = url  # 通知をタップすると予約ページが開く
        request = urllib.request.Request(
            "https://ntfy.sh/",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(request, timeout=30)
        except Exception as e:
            log(f"スマホ通知の送信に失敗: {e!r}")
    if ON_MAC:
        script = f'display notification {json.dumps(message, ensure_ascii=False)} with title {json.dumps(title, ensure_ascii=False)} sound name "Glass"'
        subprocess.run(["osascript", "-e", script], check=False)


def alert_and_offer_open(message, url):
    """見逃さないよう、閉じるまで残るダイアログも出す（待たずに次へ進む）。"""
    script = f"""
    set r to display dialog {json.dumps(message, ensure_ascii=False)} with title "スノーピーク 空きが出ました" buttons {{"閉じる", "予約ページを開く"}} default button 2
    if button returned of r is "予約ページを開く" then open location "{url}"
    """
    subprocess.Popen(["osascript", "-e", script])


def fetch_statuses(page, url):
    """対象日ごとの状態を返す。例: {"2026-11-22": "full", "2026-11-28": "available"}"""
    statuses = {}
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_selector("#room-search .c-calendarBox-daylist-month", timeout=60_000)
    for year, month in sorted({(d.year, d.month) for d in TARGET_DATES}):
        label = f"{month} {year}"
        # ページ自身の「次へ」と同じ関数で表示月を切り替える
        page.evaluate(f"changeRoomCalendar({year}{month:02d})")
        page.wait_for_function(
            """(label) => [...document.querySelectorAll('#room-search .c-calendarBox-daylist-month')]
                .some(e => e.textContent.replace(/\\s+/g, ' ').trim() === label)""",
            arg=label,
            timeout=60_000,
        )
        days = page.evaluate(READ_MONTH_JS, label)
        if days is None:
            raise RuntimeError(f"{label} のカレンダーが見つかりません")
        for d in TARGET_DATES:
            if (d.year, d.month) == (year, month):
                statuses[d.isoformat()] = days.get(str(d.day), "unknown")
    return statuses


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    if date.today() > STOP_AFTER:
        log("監視期間が終了しています。定期実行を解除してください。")
        return 0

    state = load_state()
    previous = state.get("statuses", {})
    current = {}
    failed = False

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(locale="ja-JP")
            for site in SITES:
                name, url = site["name"], site["url"]
                try:
                    statuses = fetch_statuses(page, url)
                except Exception as e:  # ネットワーク不調やページ構造の変更など
                    failed = True
                    current[name] = previous.get(name, {})  # 前回の状態を引き継ぐ
                    log(f"[{name}] 取得失敗: {e!r}")
                    continue

                current[name] = statuses
                log(f"[{name}] 状態: {statuses}")
                unknown = [d for d, s in statuses.items() if s == "unknown"]
                if unknown:
                    log(f"[{name}] 判定できない表示がありました: {unknown}（ページの作りが変わった可能性）")

                before = previous.get(name, {})
                newly_available = [
                    d for d, s in statuses.items()
                    if s.startswith("available") and not before.get(d, "").startswith("available")
                ]
                if newly_available:
                    labels = "、".join(f"{int(d[5:7])}/{int(d[8:10])}" for d in newly_available)
                    message = f"{name} {labels} に空きが出ました"
                    notify("スノーピーク 空きが出ました", message, url)
                    if ON_MAC:
                        alert_and_offer_open(message, url)
                    log(f"通知: {message}")
        finally:
            browser.close()

    failures = state.get("failures", 0) + 1 if failed else 0
    if failures == FAILURE_NOTIFY_AT:
        notify("スノーピーク監視エラー", "空き状況を連続で取得できていません。ログを確認してください。")
    save_state({"statuses": current, "failures": failures, "checked_at": datetime.now().isoformat(timespec="seconds")})
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
