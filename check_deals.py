"""
בוט דילים של tustus (ארקיע)
---------------------------
רץ ב-GitHub Actions בערך כל 10 דקות:
  • כשעולה דיל שלא הופיע קודם – שולח לטלגרם צילום מסך + שורה קצרה לכל דיל חדש.
  • כששולחים לבוט "סטטוס" – שולח צילום של האתר ורשימת כל הדילים שבו כרגע.
  • כשאין שום דבר חדש – לא שולח כלום.
הדילים שכבר נראו נשמרים בקובץ state/seen.json בריפו.

מבנה כרטיס דיל באתר (לדוגמה):
    טיסה לאתונה
    יום ג' 06/10 - יום ה' 08/10
    סה"כ
    $150
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

# ================= הגדרות =================

URL = os.environ.get("DEALS_URL", "https://www.tustus.co.il/Arkia/Home")
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
STATE_FILE = Path(os.environ.get("STATE_FILE", "state/seen.json"))
SHOTS_DIR = Path("shots")
HISTORY_LIMIT = 3000          # כמה דילים לזכור לכל היותר
STATUS_WORDS = ("סטטוס", "status")

TG = f"https://api.telegram.org/bot{TOKEN}"

# מוצא את כרטיסי הדילים בלי להסתמך על שמות class:
# הכרטיס הוא האלמנט הגדול ביותר שמכיל "טיסה ל..." אחד בלבד, תאריך ומחיר בדולרים.
FIND_CARDS_JS = r"""
() => {
  const titleRe = /טיסה ל/g;
  const dateRe = /\d{1,2}\/\d{1,2}/;
  const priceRe = /\$\s?\d/;
  const count = (t) => (t.match(titleRe) || []).length;
  const isDeal = (t) => count(t) === 1 && dateRe.test(t) && priceRe.test(t);

  const matches = [...document.querySelectorAll('body *')]
    .filter(el => isDeal(el.innerText || ''));
  const set = new Set(matches);
  // הכי קטנים: לא מכילים בתוכם עוד התאמה
  let cards = matches.filter(el => ![...el.querySelectorAll('*')].some(c => set.has(c)));
  // מטפסים למעלה כל עוד עדיין מדובר בדיל אחד בלבד – כדי לצלם את כל הכרטיס
  cards = cards.map(el => {
    let cur = el;
    while (cur.parentElement && cur.parentElement !== document.body
           && count(cur.parentElement.innerText || '') === 1) {
      cur = cur.parentElement;
    }
    return cur;
  });
  return [...new Set(cards)].map((el, i) => {
    el.setAttribute('data-dealidx', String(i));
    return { idx: i, text: el.innerText || '' };
  });
}
"""

# ================= פענוח דיל =================


def parse_deal(text: str):
    """מחזיר {'dest','dates','price'} או None."""
    dest = re.search(r"טיסה ל\s*([^\n\r]+)", text)
    dates = re.findall(r"\d{1,2}/\d{1,2}", text)
    price = re.search(r"\$\s?(\d[\d,]*)", text)
    if not (dest and dates and price):
        return None
    return {
        "dest": re.sub(r"\s+", " ", dest.group(1)).strip(),
        "dates": "–".join(dates[:2]),
        "price": "$" + price.group(1),
    }


def deal_key(d) -> str:
    return f"{d['dest']} | {d['dates']} | {d['price']}"


def short_line(d) -> str:
    return f"✈️ {d['dest']} · {d['dates']} · {d['price']}"

# ================= טלגרם =================


def tg_message(text: str):
    r = requests.post(f"{TG}/sendMessage", data={
        "chat_id": CHAT_ID, "text": text, "disable_web_page_preview": "true",
    }, timeout=30)
    if not r.ok:
        print("sendMessage failed:", r.text, file=sys.stderr)


def tg_photo(path: Path, caption: str):
    with open(path, "rb") as f:
        r = requests.post(f"{TG}/sendPhoto", data={
            "chat_id": CHAT_ID, "caption": caption[:1024],
        }, files={"photo": f}, timeout=60)
    if not r.ok:
        print("sendPhoto failed:", r.text, file=sys.stderr)
        tg_message(caption)
    time.sleep(1)


def tg_document(path: Path, caption: str = ""):
    with open(path, "rb") as f:
        r = requests.post(f"{TG}/sendDocument", data={
            "chat_id": CHAT_ID, "caption": caption[:1024],
        }, files={"document": f}, timeout=120)
    if not r.ok:
        print("sendDocument failed:", r.text, file=sys.stderr)


def tg_lines(lines, chunk=40):
    for i in range(0, len(lines), chunk):
        tg_message("\n".join(lines[i:i + chunk]))


def status_requested() -> bool:
    """האם שלחת לבוט 'סטטוס' מאז הבדיקה הקודמת. מסמן את ההודעות כנקראו."""
    try:
        r = requests.get(f"{TG}/getUpdates", params={"timeout": 0}, timeout=30).json()
    except Exception as e:
        print("getUpdates failed:", e, file=sys.stderr)
        return False
    updates = r.get("result", []) if r.get("ok") else []
    asked = False
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        if str(msg.get("chat", {}).get("id")) == str(CHAT_ID):
            text = (msg.get("text") or "").lower()
            if any(w in text for w in STATUS_WORDS):
                asked = True
    if updates:
        requests.get(f"{TG}/getUpdates",
                     params={"offset": updates[-1]["update_id"] + 1, "timeout": 0}, timeout=30)
    return asked

# ================= מצב שמור =================


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return None


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

# ================= ריצה =================


def main():
    state = load_state()
    first_run = state is None
    state = state or {"seen": []}
    SHOTS_DIR.mkdir(exist_ok=True)

    want_status = status_requested()
    print("status requested:", want_status)

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(
            viewport={"width": 1440, "height": 1400},
            locale="he-IL",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"),
        )
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)  # המתנה לטעינת הכרטיסים

        # אוספים את הדילים
        cards = page.evaluate(FIND_CARDS_JS)
        current = {}  # key -> (deal, idx)
        for c in cards:
            d = parse_deal(c["text"])
            if d and deal_key(d) not in current:
                current[deal_key(d)] = (d, c["idx"])
        print(f"found {len(cards)} cards, {len(current)} unique deals")
        for k in list(current)[:5]:
            print("  e.g.", k)

        # ----- פקודת סטטוס -----
        if want_status:
            top = SHOTS_DIR / "status.png"
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(top))
            tg_photo(top, f"📊 סטטוס tustus – כרגע {len(current)} דילים באתר\n{URL}")
            tg_lines([short_line(d) for d, _ in current.values()])
            try:
                full = SHOTS_DIR / "status_full.png"
                page.screenshot(path=str(full), full_page=True)
                tg_document(full, "צילום של כל העמוד")
            except Exception as e:
                print("full screenshot failed:", e, file=sys.stderr)

        # ----- לא נמצאו דילים: רק מדפיסים לאבחון, לא שולחים כלום -----
        if not current:
            body = page.inner_text("body")
            print("DEBUG title:", page.title())
            print("DEBUG url:", page.url)
            print("DEBUG body length:", len(body))
            print("DEBUG body start:\n", body[:1500])
            page.screenshot(path=str(SHOTS_DIR / "debug.png"))
            browser.close()
            return

        # ----- דילים חדשים -----
        seen = set(state.get("seen", []))
        new_keys = [k for k in current if k not in seen]
        print(f"{len(new_keys)} new deals")

        # בהרצה הראשונה רק לומדים מה כבר קיים – בלי התראה
        if new_keys and not first_run:
            shot = SHOTS_DIR / "new.png"
            try:
                first_idx = current[new_keys[0]][1]
                page.locator(f'[data-dealidx="{first_idx}"]').scroll_into_view_if_needed(timeout=10000)
            except Exception as e:
                print("scroll failed:", e, file=sys.stderr)
            page.screenshot(path=str(shot))

            lines = [short_line(current[k][0]) for k in new_keys]
            text = f"🔥 {len(new_keys)} חדש ב-tustus\n" + "\n".join(lines) + f"\n{URL}"
            if len(text) <= 1024:
                tg_photo(shot, text)
            else:
                tg_photo(shot, f"🔥 {len(new_keys)} דילים חדשים ב-tustus\n{URL}")
                tg_lines(lines)

        browser.close()

    # זוכרים כל דיל שאי פעם נראה, כדי לא להתריע שוב על אותו דיל
    history = [k for k in state.get("seen", []) if k not in current] + sorted(current)
    state["seen"] = history[-HISTORY_LIMIT:]
    save_state(state)


if __name__ == "__main__":
    main()
