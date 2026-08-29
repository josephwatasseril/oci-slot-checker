import os
import sys
import requests
import calendar
from datetime import datetime, date
from playwright.sync_api import sync_playwright

# --- Configuration ---
NTFY_TOPIC = os.getenv("NTFY_TOPIC")

if not NTFY_TOPIC:
    raise ValueError("NTFY_TOPIC environment variable is not set.")

START_DATE = date(2026, 9, 3)
CUTOFF_DATE = date(2026, 10, 15)
EXCLUDED_DATES = {
    date(2026, 9, 11),
    date(2026, 9, 14),
    date(2026, 9, 22),
    date(2026, 9, 23)
}

def get_months_to_scan(start: date, end: date):
    """Generates a list of (year, month_1_indexed) tuples between start and end."""
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

def send_phone_push(slots):
    message = f"Matching OCI slots available: {', '.join(slots)}"
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": "🚨 OCI Slot Found (Berlin)",
                "Priority": "urgent",
                "Tags": "rotating_light,calendar",
                "Click": "https://appointment.indianembassyberlin.gov.in"
            },
            timeout=10
        )
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Push notification sent to phone.")
    except Exception as e:
        print(f"Failed to send push notification: {e}")

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

def run_check():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting Embassy check...")
    
    months_to_scan = get_months_to_scan(START_DATE, CUTOFF_DATE)
    print(f"Target Window: {START_DATE} to {CUTOFF_DATE}")
    print(f"Months to dynamically scan: {[f'{calendar.month_name[m]} {y}' for y, m in months_to_scan]}")
    
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
        
        try:
            page.goto("https://appointment.indianembassyberlin.gov.in", timeout=30000, wait_until="domcontentloaded")
            
            # Step 1: Initial Terms Agreement Screen
            if page.locator("#agree").count() > 0 and page.locator("#dropdown").count() == 0:
                print("Step 1: Accepting initial terms...")
                page.check("#agree")
                page.click("#btnSubmit")
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(1000)
            
            # Step 2: Jurisdiction Selection Screen (Berlin)
            if page.locator("#dropdown").count() > 0:
                print("Step 2: Selecting Berlin jurisdiction...")
                page.select_option("#dropdown", label="Berlin")
                page.evaluate("document.querySelector('#dropdown').dispatchEvent(new Event('change', {bubbles: true}))")
                
                page.check("#agree")
                page.evaluate("document.querySelector('#agree').dispatchEvent(new Event('change', {bubbles: true}))")
                page.wait_for_timeout(500)
                
                page.evaluate("document.querySelector('#btnSubmit').removeAttribute('disabled')")
                page.click("#btnSubmit")
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(1500)
            
            # Step 3: Main Appointment Form Selection
            print("Step 3: Loading appointment form...")
            page.wait_for_selector("#category", timeout=15000)
            page.select_option("#category", "1")
            page.evaluate("if (typeof refreshDependent === 'function') refreshDependent();")
            
            page.wait_for_selector("#service", timeout=15000)
            page.wait_for_timeout(1000)
            page.select_option("#service", "20")
            
            # Step 4: Open Datepicker
            page.wait_for_selector("#appmnt_date", timeout=15000)
            page.wait_for_timeout(1000)
            
            page.evaluate("""() => {
                if (window.jQuery && window.jQuery('#appmnt_date').length) {
                    window.jQuery('#appmnt_date').datepicker('show');
                } else {
                    const input = document.getElementById('appmnt_date');
                    if (input) { input.focus(); input.click(); }
                    const addon = document.querySelector('.input-group-addon');
                    if (addon) addon.click();
                }
            }""")
            
            page.wait_for_selector("#ui-datepicker-div", state="visible", timeout=10000)
            
            # Step 5: Dynamic Multi-Month Iteration
            all_matches = []
            for scan_year, scan_month in months_to_scan:
                print(f"Scanning {calendar.month_name[scan_month]} {scan_year}...")
                switch_datepicker_view(page, scan_year, scan_month)
                page.wait_for_timeout(1200)
                all_matches.extend(parse_and_check_month(page, scan_year, scan_month))
            
            # Step 6: Capture Proof of Completion Screenshot
            page.screenshot(path="completion_screenshot.png", full_page=True)
            print("Saved completion screenshot to completion_screenshot.png")
            
            # Step 7: Handle Results
            if all_matches:
                print(f"🎉 MATCH FOUND: {all_matches}")
                send_phone_push(all_matches)
            else:
                print(f"No matching slots found between {START_DATE} and {CUTOFF_DATE}.")
                
        except Exception as err:
            print(f"Error during execution: {err}")
            try:
                page.screenshot(path="error_screenshot.png", timeout=5000)
            except Exception:
                pass
            sys.exit(1)
        finally:
            browser.close()

if __name__ == "__main__":
    run_check()