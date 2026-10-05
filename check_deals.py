"""
בודק דילים חדשים ב-tustus (ארקיע) ושולח לטלגרם צילום מסך והודעה קצרה רק כשעולים דילים חדשים.
רץ ב-GitHub Actions; את הדילים שכבר נראו שומר ב-state/seen.json.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

URL = os.environ.get("DEALS_URL", "https://www.tustus.co.il/Arkia/Home")
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
STATE_FILE = Path(os.environ.get("STATE_FILE", "state/seen.json"))
SHOTS_DIR = Path("shots")

TG = f"https://api.telegram.org/bot{TOKEN}"

# מוצא את "כרטיסי" הדילים בעמוד בלי להסתמך על שמות class:
# אלמנט שמכיל כותרת "טיסה ל... DD/MM" וגם מחיר בדולרים, ולא מכיל עוד כותרת כזו.
FIND_CARDS_JS = r"""
() => {
  const titleRe = /טיסה ל[^\n]*?\d{1,2}\/\d{1,2}/g;
  const priceRe = /\$\s?[\d,]+|[\d,]+\s?\$/;
  const count = (t) => (t.match(titleRe) || []).length;
  const els = [...document.querySelectorAll('body *')];
  const matches = els.filter(el => {
    const t = el.innerText || '';
    return t.length < 4000 && count(t) >= 1 && priceRe.test(t);
  });
  const set = new Set(matches);
  let cards = matches.filter(el => ![...el.querySelectorAll('*')].some(c => set.has(c)));
  // מטפסים למעלה כדי לתפוס את כל הכרטיס (כולל פרטי הטיסות), כל עוד יש בו דיל אחד בלבד
  cards = cards.map(el => {
    let cur = el;
    while (cur.parentElement && cur.parentElement !== document.body) {
      const pt = cur.parentElement.innerText || '';
      if (count(pt) !== 1 || pt.length > 3000) break;
      cur = cur.parentElement;
    }
    return cur;
  });
  const uniq = [...new Set(cards)];
  return uniq.map((el, i) => {
    el.setAttribute('data-dealidx', String(i));
    return { idx: i, text: el.innerText || '' };
  });
}
"""


def deal_key(text: str):
    title = re.search(r"טיסה ל[^\n]*?\d{1,2}/\d{1,2}[^\n]*", text)
    price = (re.search(r"\$ ?\d[\d,]*", text)
             or re.search(r"\d[\d,]* ?(?:\$|₪|דולר)", text))
    if not title or not price:
        return None
    t = re.sub(r"\s+", " ", title.group(0)).strip()
    p = re.sub(r"\s+", "", price.group(0))
    return f"{t} | {p}"


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
    time.sleep(1)  # לא להציף את טלגרם


def tg_document(path: Path, caption: str = ""):
    with open(path, "rb") as f:
        r = requests.post(f"{TG}/sendDocument", data={
            "chat_id": CHAT_ID, "caption": caption[:1024],
        }, files={"document": f}, timeout=120)
    if not r.ok:
        print("sendDocument failed:", r.text, file=sys.stderr)


STATUS_WORDS = ("סטטוס", "status", "/status")


def status_requested() -> bool:
    """בודק אם שלחת לבוט 'סטטוס' מאז ההרצה הקודמת, ומסמן את ההודעות כנקראו."""
    try:
        r = requests.get(f"{TG}/getUpdates", params={"timeout": 0}, timeout=30).json()
    except Exception as e:
        print("getUpdates failed:", e, file=sys.stderr)
        return False
    updates = r.get("result", []) if r.get("ok") else []
    asked = False
    for u in updates:
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) == str(CHAT_ID):
            if any(w in (msg.get("text") or "").lower() for w in STATUS_WORDS):
                asked = True
    if updates:  # מסמנים כנקראו כדי לא לענות שוב
        requests.get(f"{TG}/getUpdates",
                     params={"offset": updates[-1]["update_id"] + 1, "timeout": 0}, timeout=30)
    return asked


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return None


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def short_line(key: str) -> str:
    """'טיסה ללרנקה יום ב' 05/10 - יום ד' 07/10 | $100' -> '✈️ לרנקה · 05/10–07/10 · $100'"""
    title, _, price = key.partition(" | ")
    m = re.search(r"טיסה ל(.+?)(?:\s+יום|\s+\d{1,2}/|$)", title)
    dest = m.group(1).strip() if m else title
    dates = re.findall(r"\d{1,2}/\d{1,2}", title)
    when = "–".join(dates[:2])
    return f"✈️ {dest} · {when} · {price}"


def main():
    state = load_state()
    first_run = state is None
    state = state or {"seen": []}
    SHOTS_DIR.mkdir(exist_ok=True)
    want_status = status_requested()
    print("status requested:", want_status)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1280, "height": 1600},
            locale="he-IL",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
        )
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)  # המתנה לטעינה גרפית

        cards = page.evaluate(FIND_CARDS_JS)
        current = {}
        for c in cards:
            k = deal_key(c["text"])
            if k and k not in current:
                current[k] = c["idx"]

        print(f"found {len(cards)} cards, {len(current)} unique deals")

        if want_status:
            top = SHOTS_DIR / "status.png"
            full = SHOTS_DIR / "status_full.png"
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(top))
            lines = [short_line(k) for k in current]
            cap = f"📊 סטטוס tustus – כרגע {len(current)} דילים באתר"
            tg_photo(top, cap)
            for i in range(0, len(lines), 40):
                tg_message("\n".join(lines[i:i + 40]))
            try:
                page.screenshot(path=str(full), full_page=True)
                tg_document(full, "צילום של כל העמוד")
            except Exception as e:
                print("full screenshot failed:", e, file=sys.stderr)

        if not current:
            # אין דילים (או שהעמוד לא נטען) – לא שולחים כלום
            browser.close()
            return

        seen = set(state.get("seen", []))
        new_keys = [k for k in current if k not in seen]
        print(f"{len(new_keys)} new deals")

        # בהרצה הראשונה רק לומדים מה קיים כבר – בלי התראה
        if new_keys and not first_run:
            shot = SHOTS_DIR / "new.png"
            try:
                # צילום של אזור הדיל החדש הראשון (כולל מה שסביבו)
                page.locator(f'[data-dealidx="{current[new_keys[0]]}"]').scroll_into_view_if_needed(timeout=10000)
                page.screenshot(path=str(shot))
            except Exception as e:
                print("screenshot failed:", e, file=sys.stderr)
                page.screenshot(path=str(shot))
            lines = [short_line(k) for k in new_keys]
            text = f"🔥 {len(new_keys)} חדש ב-tustus\n" + "\n".join(lines) + f"\n{URL}"
            if len(text) <= 1024:
                tg_photo(shot, text)
            else:
                tg_photo(shot, f"🔥 {len(new_keys)} דילים חדשים ב-tustus")
                for i in range(0, len(lines), 40):
                    tg_message("\n".join(lines[i:i + 40]))
                tg_message(URL)

        browser.close()

    # זוכרים כל דיל שאי פעם נראה, כדי לא להתריע שוב על אותו דיל (עד 3000 אחרונים)
    history = [k for k in state.get("seen", []) if k not in current] + sorted(current.keys())
    state["seen"] = history[-3000:]
    save_state(state)


if __name__ == "__main__":
    main()
