import os
import sys
import json
import time
import requests
from datetime import datetime, date
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

# --- Configuration & Environment Variables ---
BERLIN_TZ = ZoneInfo("Europe/Berlin")

def get_env_date(var_name: str, fallback: str) -> date:
    raw = os.getenv(var_name, fallback).strip()
    return datetime.strptime(raw, "%Y-%m-%d").date()

def get_env_date_set(var_name: str, fallback: str = "") -> set:
    raw = os.getenv(var_name, fallback).strip()
    if not raw:
        return set()
    return {datetime.strptime(d.strip(), "%Y-%m-%d").date() for d in raw.split(",") if d.strip()}

NTFY_TOPIC = os.getenv("NTFY_TOPIC")
if not NTFY_TOPIC:
    print("Error: NTFY_TOPIC environment variable is not set.")
    sys.exit(1)

START_DATE = get_env_date("START_DATE", "2026-09-03")
CUTOFF_DATE = get_env_date("CUTOFF_DATE", "2026-10-15")
EXCLUDED_DATES = get_env_date_set("EXCLUDED_DATES", "2026-09-11,2026-09-14,2026-09-22,2026-09-23")
PREFERRED_SLOT_COUNT = int(os.getenv("PREFERRED_SLOT_COUNT", "2"))

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

CACHE_DIR = ".cache"
STATE_FILE = os.path.join(CACHE_DIR, "state.json")
COOLDOWN_SECONDS = 1 * 3600

# --- Heartbeat Window Configuration ---
HEARTBEAT_START_HOUR = int(os.getenv("HEARTBEAT_START_HOUR", "8"))
HEARTBEAT_END_HOUR = int(os.getenv("HEARTBEAT_END_HOUR", "10"))

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
    
    if HEARTBEAT_START_HOUR <= now_berlin.hour < HEARTBEAT_END_HOUR and state.get("last_heartbeat_date") != today_str:
        try:
            payload = {
                "topic": NTFY_TOPIC,
                "title": "OCI Bot Status: Healthy",
                "message": f"Bot active. Range: {START_DATE} to {CUTOFF_DATE} (Target: {PREFERRED_SLOT_COUNT}+ slots).",
                "priority": 1,
                "tags": ["white_check_mark"],
                "click": "https://appointment.indianembassyberlin.gov.in"
            }
            requests.post("https://ntfy.sh", json=payload, timeout=10)
            state["last_heartbeat_date"] = today_str
            print(f"[{now_berlin.strftime('%H:%M:%S')}] Daily heartbeat sent.")
        except Exception as e:
            print(f"Failed to send heartbeat: {e}")

def send_slot_alert(matches: list, is_new=True, screenshot_path="completion_screenshot.png"):
    # Format message body with breakdown per date
    lines = []
    has_preferred_match = False
    
    for match in matches:
        d_str = match["date"]
        times = match["available_times"]
        count = len(times)
        if count >= PREFERRED_SLOT_COUNT:
            has_preferred_match = True
            lines.append(f"🎯 {d_str} ({count} slots): {', '.join(times)}")
        else:
            lines.append(f"ℹ️ {d_str} ({count} slot): {', '.join(times)}")
            
    summary_body = "\n".join(lines)
    
    if has_preferred_match:
        title = f"🎯 IDEAL OCI MATCH ({PREFERRED_SLOT_COUNT}+ Slots Found!)"
        priority = "urgent"
        tags = "rotating_light,tada,calendar"
    else:
        title = f"OCI Slots Available ({len(matches)} dates found)"
        priority = "high"
        tags = "calendar,bell"

    headers = {
        "Title": title,
        "Priority": priority if is_new else "high",
        "Tags": tags,
        "Click": "https://appointment.indianembassyberlin.gov.in",
        "Actions": "view, Open Booking Portal, https://appointment.indianembassyberlin.gov.in"
    }

    try:
        if os.path.exists(screenshot_path):
            headers["Filename"] = "slots.png"
            with open(screenshot_path, "rb") as img:
                requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=img, headers=headers, timeout=15)
        else:
            requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=summary_body.encode("utf-8"), headers=headers, timeout=10)
        print("Slot alert push notification sent.")
    except Exception as e:
        print(f"Failed to send slot alert: {e}")

def handle_alert_deduplication(all_matches, state, screenshot_path):
    if not all_matches:
        print(f"No matching slots found between {START_DATE} and {CUTOFF_DATE}.")
        state["last_seen_slots"] = []
        return

    # Extract signature (e.g. ["2026-09-18: 1018 – 1030, 1030 – 1042"])
    current_signatures = [f"{m['date']}: {','.join(m['available_times'])}" for m in all_matches]
    cached_signatures = set(state.get("last_seen_slots", []))
    
    new_found = set(current_signatures) - cached_signatures
    current_time = time.time()
    time_since_last_alert = current_time - state.get("last_alert_timestamp", 0)

    if new_found:
        print(f"🎉 NEW SLOTS DISCOVERED: {new_found}")
        send_slot_alert(all_matches, is_new=True, screenshot_path=screenshot_path)
        state["last_alert_timestamp"] = current_time
    elif time_since_last_alert >= COOLDOWN_SECONDS:
        print(f"Cooldown elapsed. Sending reminder alert.")
        send_slot_alert(all_matches, is_new=False, screenshot_path=screenshot_path)
        state["last_alert_timestamp"] = current_time
    else:
        remaining_mins = int((COOLDOWN_SECONDS - time_since_last_alert) // 60)
        print(f"Available slots already notified. Suppressed by cooldown ({remaining_mins}m left).")

    state["last_seen_slots"] = current_signatures

# --- Helper Functions ---
def get_months_to_scan(start: date, end: date):
    months = []
    curr_y, curr_m = start.year, start.month
    end_y, end_m = end.year, end.month
    while (curr_y < end_y) or (curr_y == end_y and curr_m <= end_m):
        months.append((curr_y, curr_m))
        curr_m += 1
        if curr_m > 12:
            curr_m = 1
            curr_y += 1
    return months

def switch_datepicker_view(page, year: int, month_1_indexed: int):
    m_val = str(month_1_indexed - 1)
    y_val = str(year)
    page.evaluate("""({mVal, yVal}) => {
        const jq = window.jQuery;
        if (jq) {
            const $y = jq('#ui-datepicker-div select.ui-datepicker-year');
            const $m = jq('#ui-datepicker-div select.ui-datepicker-month');
            if ($y.length && $y.val() !== yVal) $y.val(yVal).trigger('change');
            if ($m.length) $m.val(mVal).trigger('change');
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
    }""", {"mVal": m_val, "yVal": y_val})

def extract_day_timeslots(page):
    """Parses enabled timeslots from #timeslots."""
    try:
        page.wait_for_selector("#timeslots", timeout=4000)
        page.wait_for_timeout(400)  # Allow radio population
        
        # Select active radios
        active_radios = page.query_selector_all("#timeslots input[type='radio']:not([disabled])")
        times = []
        for radio in active_radios:
            # Get parent <li> text (e.g. "1018 – 1030 (Available)")
            li_handle = radio.evaluate_handle("el => el.closest('li')")
            if li_handle:
                raw_text = li_handle.as_element().inner_text()
                time_str = raw_text.replace("(Available)", "").replace("\n", " ").strip()
                if time_str:
                    times.append(time_str)
        return times
    except Exception as e:
        print(f"Could not read timeslots: {e}")
        return []

def scan_calendar_for_slots(page, months_to_scan):
    all_matches = []
    
    for scan_year, scan_month in months_to_scan:
        switch_datepicker_view(page, scan_year, scan_month)
        page.wait_for_timeout(400)
        page.wait_for_selector("#ui-datepicker-div tbody", timeout=8000)
        
        cells = page.query_selector_all("#ui-datepicker-div tbody td")
        candidate_days = []
        
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
                
            slot_date = date(scan_year, scan_month, int(day_text))
            if START_DATE <= slot_date < CUTOFF_DATE and slot_date not in EXCLUDED_DATES:
                candidate_days.append((slot_date, day_text))

        # Click into each open date to read specific timeslots
        for slot_date, day_str in candidate_days:
            date_str = slot_date.strftime("%Y-%m-%d")
            print(f"Checking timeslots for open date: {date_str}...")
            
            # Click the date link in datepicker
            day_locator = page.locator(f"#ui-datepicker-div td:not(.other-month) a:text-is('{day_str}')").first
            if day_locator.count() > 0:
                day_locator.click()
                available_times = extract_day_timeslots(page)
                
                if available_times:
                    print(f"  -> Found {len(available_times)} active slot(s): {available_times}")
                    all_matches.append({
                        "date": date_str,
                        "available_times": available_times
                    })
                
                # Reopen datepicker for next scan
                page.evaluate("if(window.jQuery) window.jQuery('#appmnt_date').datepicker('show');")
                page.wait_for_timeout(300)
                switch_datepicker_view(page, scan_year, scan_month)

    return all_matches

def execute_scrape_cycle(page, months_to_scan):
    """Executes a single end-to-end check cycle."""
    # Step 0: Open Site with retry fallback
    page.goto(
        "https://appointment.indianembassyberlin.gov.in",
        timeout=25000,
        wait_until="domcontentloaded"
    )
    
    # Step 1: Initial Terms
    if page.locator("#agree").count() > 0 and page.locator("#dropdown").count() == 0:
        page.check("#agree")
        page.click("#btnSubmit")
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(600)
    
    # Step 2: Jurisdiction (Berlin)
    if page.locator("#dropdown").count() > 0:
        page.select_option("#dropdown", label="Berlin")
        page.evaluate("document.querySelector('#dropdown').dispatchEvent(new Event('change', {bubbles: true}))")
        page.check("#agree")
        page.evaluate("document.querySelector('#agree').dispatchEvent(new Event('change', {bubbles: true}))")
        page.evaluate("document.querySelector('#btnSubmit').removeAttribute('disabled')")
        page.click("#btnSubmit")
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(800)
    
    # Step 3: Main Appointment Form Selection
    page.wait_for_selector("#category", timeout=15000)
    page.select_option("#category", "1")
    page.evaluate("if (typeof refreshDependent === 'function') refreshDependent();")
    
    page.wait_for_selector("#service", timeout=15000)
    page.wait_for_timeout(400)
    page.select_option("#service", "20")
    
    # Step 4: Open Datepicker
    page.wait_for_selector("#appmnt_date", timeout=15000)
    page.wait_for_timeout(400)
    page.evaluate("""() => {
        if (window.jQuery && window.jQuery('#appmnt_date').length) {
            window.jQuery('#appmnt_date').datepicker('show');
        } else {
            const input = document.getElementById('appmnt_date');
            if (input) { input.focus(); input.click(); }
        }
    }""")
    page.wait_for_selector("#ui-datepicker-div", state="visible", timeout=10000)
    
    # Step 5: Scan Months & Extract Timeslots
    return scan_calendar_for_slots(page, months_to_scan)
    
# --- Main Runner ---
def run_check():
    state = load_state()
    send_heartbeat(state)
    
    print(f"[{datetime.now(BERLIN_TZ).strftime('%Y-%m-%d %H:%M:%S')} CET] Running check for {START_DATE} to {CUTOFF_DATE}...")
    months_to_scan = get_months_to_scan(START_DATE, CUTOFF_DATE)
    
    all_matches = None
    screenshot_path = "completion_screenshot.png"

    with sync_playwright() as p:
        for attempt in range(1, MAX_RETRIES + 1):
            browser = None
            try:
                print(f"[{datetime.now(BERLIN_TZ).strftime('%H:%M:%S')}] Attempt {attempt}/{MAX_RETRIES}...")
                browser = p.chromium.launch(
                    channel="chrome",  # Uses GitHub Actions pre-installed Google Chrome
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
                page.route("**/*", lambda r: r.abort() if r.request.resource_type in ["image", "media", "font"] else r.continue_())

                all_matches = execute_scrape_cycle(page, months_to_scan)
                page.screenshot(path=screenshot_path, full_page=True)
                break  # Successful run -> break out of retry loop

            except Exception as err:
                print(f"⚠️ Attempt {attempt}/{MAX_RETRIES} encountered an issue: {err}")
                if attempt == MAX_RETRIES:
                    try:
                        if 'page' in locals() and page:
                            page.screenshot(path="error_screenshot.png", timeout=5000)
                    except Exception:
                        pass
                    save_state(state)
                    sys.exit(1)
                
                # Exponential backoff before next attempt
                backoff_seconds = attempt * 3
                print(f"Retrying in {backoff_seconds}s with fresh browser context...")
                time.sleep(backoff_seconds)
            finally:
                if browser:
                    browser.close()

    if all_matches is not None:
        handle_alert_deduplication(all_matches, state, screenshot_path)
        save_state(state)

if __name__ == "__main__":
    run_check()
