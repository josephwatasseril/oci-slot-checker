import os
import sys
import json
import time
import requests
import calendar
from datetime import datetime, date
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

# --- Configuration ---
NTFY_TOPIC = os.getenv("NTFY_TOPIC")
if not NTFY_TOPIC:
    print("Error: NTFY_TOPIC environment variable is not set.")
    sys.exit(1)

START_DATE = date(2026, 9, 3)
CUTOFF_DATE = date(2026, 10, 15)
EXCLUDED_DATES = {
    date(2026, 9, 11),
    date(2026, 9, 14),
    date(2026, 9, 22),
    date(2026, 9, 23)
}

BERLIN_TZ = ZoneInfo("Europe/Berlin")
CACHE_DIR = ".cache"
STATE_FILE = os.path.join(CACHE_DIR, "state.json")
COOLDOWN_SECONDS = 1 * 3600  # 1 hour cooldown for identical slots

# --- State Management ---
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Could not read state file: {e}")
    return {"last_heartbeat_date": None, "last_seen_slots": [], "last_alert_timestamp": 0}

def save_state(state):
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"Warning: Could not save state: {e}")

# --- Notifications ---
def send_heartbeat(state):
    now_berlin = datetime.now(BERLIN_TZ)
    today_str = now_berlin.strftime("%Y-%m-%d")
    
    # Send daily heartbeat between 08:00 and 10:00 Berlin time if not already sent today
    if 8 <= now_berlin.hour < 11 and state.get("last_heartbeat_date") != today_str:
        try:
            # Using JSON payload avoids all HTTP header encoding limitations
            payload = {
                "topic": NTFY_TOPIC,
                "title": "OCI Bot Status: Healthy",
                "message": f"Bot active. Daily health check passed at {now_berlin.strftime('%H:%M')} CET.",
                "priority": 1,  # low priority
                "tags": ["white_check_mark"],
                "click": "https://appointment.indianembassyberlin.gov.in"
            }
            response = requests.post("https://ntfy.sh", json=payload, timeout=10)
            response.raise_for_status()
            
            state["last_heartbeat_date"] = today_str
            print(f"[{now_berlin.strftime('%H:%M:%S')}] Daily heartbeat sent successfully.")
        except Exception as e:
            print(f"Failed to send heartbeat: {e}")

def send_slot_alert(slots, is_new=True, screenshot_path="completion_screenshot.png"):
    title = f"OCI Slot Found ({len(slots)} available)" if is_new else f"Reminder: OCI Slots Available ({len(slots)})"
    message = f"Matching dates: {', '.join(slots)}"
    
    # Headers must remain pure ASCII (emojis provided via Tags)
    headers = {
        "Title": title,
        "Priority": "urgent" if is_new else "high",
        "Tags": "rotating_light,calendar" if is_new else "bell,calendar",
        "Click": "https://appointment.indianembassyberlin.gov.in",
        "Actions": "view, Open Embassy Portal, https://appointment.indianembassyberlin.gov.in"
    }

    try:
        if os.path.exists(screenshot_path):
            headers["Filename"] = "slots.png"
            with open(screenshot_path, "rb") as img:
                requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=img, headers=headers, timeout=15)
        else:
            requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"), headers=headers, timeout=10)
        print("Slot alert push notification sent successfully.")
    except Exception as e:
        print(f"Failed to send slot alert: {e}")

def handle_alert_deduplication(all_matches, state, screenshot_path):
    current_time = time.time()
    cached_slots = set(state.get("last_seen_slots", []))
    current_slots = set(all_matches)
    
    new_slots = current_slots - cached_slots
    time_since_last_alert = current_time - state.get("last_alert_timestamp", 0)

    if new_slots:
        print(f"🎉 NEW SLOTS DISCOVERED: {list(new_slots)}")
        send_slot_alert(all_matches, is_new=True, screenshot_path=screenshot_path)
        state["last_alert_timestamp"] = current_time
    elif current_slots and time_since_last_alert >= COOLDOWN_SECONDS:
        print(f"Cooldown elapsed ({COOLDOWN_SECONDS // 3600}h). Sending reminder for available slots: {all_matches}")
        send_slot_alert(all_matches, is_new=False, screenshot_path=screenshot_path)
        state["last_alert_timestamp"] = current_time
    elif current_slots:
        remaining_mins = int((COOLDOWN_SECONDS - time_since_last_alert) // 60)
        print(f"Slots {all_matches} already notified. Cooldown active ({remaining_mins}m remaining). Suppressing repeat alert.")

    state["last_seen_slots"] = all_matches

# --- Helper Functions ---
def get_months_to_scan(start: date, end: date):
    months = []
    curr_year, curr_month = start.year, start.month
    end_year, end_month = end.year, end.month
    
    while (curr_year < end_year) or (curr_year == end_year and curr_month <= end_month):
        months.append((curr_year, curr_month))
        curr_month += 1
        if curr_month > 12:
            curr_month = 1
            curr_year += 1
    return months

def parse_and_check_month(page, year: int, month_idx: int):
    found_slots = []
    page.wait_for_selector("#ui-datepicker-div tbody", timeout=10000)
    cells = page.query_selector_all("#ui-datepicker-div tbody td")
    
    for cell in cells:
        classes = cell.get_attribute("class") or ""
        if any(b in classes for b in ["ui-datepicker-unselectable", "ui-state-disabled", "booked-dates", "weekends", "other-month"]):
            continue
            
        link = cell.query_selector("a")
        if not link:
            continue
            
        day_text = link.inner_text().strip()
        if not day_text.isdigit():
            continue
            
        slot_date = date(year, month_idx, int(day_text))
        if START_DATE <= slot_date < CUTOFF_DATE and slot_date not in EXCLUDED_DATES:
            found_slots.append(slot_date.strftime("%Y-%m-%d"))
            
    return found_slots

def switch_datepicker_view(page, year: int, month_1_indexed: int):
    jquery_month_val = str(month_1_indexed - 1)
    jquery_year_val = str(year)
    
    page.evaluate("""({mVal, yVal}) => {
        const jq = window.jQuery;
        if (jq) {
            const $y = jq('#ui-datepicker-div select.ui-datepicker-year');
            const $m = jq('#ui-datepicker-div select.ui-datepicker-month');
            if ($y.length && $y.val() !== yVal) {
                $y.val(yVal).trigger('change');
            }
            if ($m.length) {
                $m.val(mVal).trigger('change');
            }
        } else {
            const ySel = document.querySelector('#ui-datepicker-div select.ui-datepicker-year');
            const mSel = document.querySelector('#ui-datepicker-div select.ui-datepicker-month');
            if (ySel && ySel.value !== yVal) {
                ySel.value = yVal;
                ySel.dispatchEvent(new Event('change', { bubbles: true }));
            }
            if (mSel) {
                mSel.value = mVal;
                mSel.dispatchEvent(new Event('change', { bubbles: true }));
            }
        }
    }""", {"mVal": jquery_month_val, "yVal": jquery_year_val})

# --- Main Runner ---
def run_check():
    state = load_state()
    send_heartbeat(state)
    
    print(f"[{datetime.now(BERLIN_TZ).strftime('%Y-%m-%d %H:%M:%S')} CET] Starting Embassy check...")
    months_to_scan = get_months_to_scan(START_DATE, CUTOFF_DATE)
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            locale="en-GB",
            timezone_id="Europe/Berlin"
        )
        page = context.new_page()
        
        # Abort heavy media requests to optimize run duration
        page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media", "font"] else route.continue_())
        
        try:
            # Step 0: Open Site
            page.goto("https://appointment.indianembassyberlin.gov.in", timeout=30000, wait_until="domcontentloaded")
            
            # Step 1: Initial Terms
            if page.locator("#agree").count() > 0 and page.locator("#dropdown").count() == 0:
                page.check("#agree")
                page.click("#btnSubmit")
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(800)
            
            # Step 2: Jurisdiction (Berlin)
            if page.locator("#dropdown").count() > 0:
                page.select_option("#dropdown", label="Berlin")
                page.evaluate("document.querySelector('#dropdown').dispatchEvent(new Event('change', {bubbles: true}))")
                page.check("#agree")
                page.evaluate("document.querySelector('#agree').dispatchEvent(new Event('change', {bubbles: true}))")
                page.evaluate("document.querySelector('#btnSubmit').removeAttribute('disabled')")
                page.click("#btnSubmit")
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(1000)
            
            # Step 3: Main Appointment Form Selection
            page.wait_for_selector("#category", timeout=15000)
            page.select_option("#category", "1")
            page.evaluate("if (typeof refreshDependent === 'function') refreshDependent();")
            
            page.wait_for_selector("#service", timeout=15000)
            page.wait_for_timeout(500)
            page.select_option("#service", "20")
            
            # Step 4: Open Datepicker
            page.wait_for_selector("#appmnt_date", timeout=15000)
            page.wait_for_timeout(500)
            page.evaluate("""() => {
                if (window.jQuery && window.jQuery('#appmnt_date').length) {
                    window.jQuery('#appmnt_date').datepicker('show');
                } else {
                    const input = document.getElementById('appmnt_date');
                    if (input) { input.focus(); input.click(); }
                }
            }""")
            
            page.wait_for_selector("#ui-datepicker-div", state="visible", timeout=10000)
            
            # Step 5: Scan Months
            all_matches = []
            for scan_year, scan_month in months_to_scan:
                switch_datepicker_view(page, scan_year, scan_month)
                page.wait_for_timeout(500)
                all_matches.extend(parse_and_check_month(page, scan_year, scan_month))
            
            # Step 6: Save Proof Screenshot
            screenshot_path = "completion_screenshot.png"
            page.screenshot(path=screenshot_path, full_page=True)
            
            # Step 7: Handle Alerting & Deduplication
            handle_alert_deduplication(all_matches, state, screenshot_path)
            
            if not all_matches:
                print(f"No matching slots found between {START_DATE} and {CUTOFF_DATE}.")
                
        except Exception as err:
            print(f"Error during execution: {err}")
            try:
                page.screenshot(path="error_screenshot.png", timeout=5000)
            except Exception:
                pass
            sys.exit(1)
        finally:
            save_state(state)
            browser.close()

if __name__ == "__main__":
    run_check()
