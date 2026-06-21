"""
ibkr_sms_auto_login.py v2
iOS Shortcuts -> Railway /api/sms-code -> IBKR auto-login + document upload.
"""
from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
import time, urllib.request, json, pyotp

IBKR_USER = "takuma2ai9"
IBKR_PASS = "Ibkr2AI2025!"
IBKR_LOGIN_URL = "https://www.interactivebrokers.com/sso/Login?action=DOC"
RAILWAY_SMS_URL = "https://orchestrator-production-61d8.up.railway.app/api/sms-code"


def wait_for_sms_code(timeout=180):
    """Poll Railway endpoint until a fresh SMS code appears (within last 5 min)."""
    import datetime
    print(f"Waiting for IBKR SMS code via Railway (max {timeout}s)...")
    deadline = time.time() + timeout
    # Clear any stale code first
    try:
        r = urllib.request.urlopen(RAILWAY_SMS_URL, timeout=5)
        prev = json.loads(r.read()).get("time")
    except Exception:
        prev = None

    while time.time() < deadline:
        try:
            r = urllib.request.urlopen(RAILWAY_SMS_URL, timeout=5)
            data = json.loads(r.read())
            code = data.get("code")
            ts = data.get("time")
            if code and ts and ts != prev:
                # Check freshness: within last 5 minutes
                t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age = (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds()
                if age < 300:
                    print(f"SMS code received: {code} (age: {age:.0f}s)")
                    return code
        except Exception as e:
            pass
        time.sleep(3)
    return None


def ibkr_login_with_sms():
    opts = Options()
    opts.add_argument("--no-sandbox")
    driver = webdriver.Edge(options=opts)
    try:
        driver.get(IBKR_LOGIN_URL)
        time.sleep(3)

        driver.find_element(By.NAME, "username").send_keys(IBKR_USER)
        driver.find_element(By.NAME, "password").send_keys(IBKR_PASS)
        driver.find_element(By.XPATH, '//button[@type="submit"]').click()
        time.sleep(4)
        print("Credentials submitted — SMS will be sent to phone ending 9000")

        sms_code = wait_for_sms_code(timeout=180)
        if not sms_code:
            print("ERROR: SMS code not received within 3 minutes")
            driver.quit()
            return None

        # Enter code in temp-response field
        driver.execute_script("""
            var fields = ['temp-response', 'bronze-response', 'silver-response'];
            for(var name of fields) {
                var f = document.querySelector('[name=' + name + ']');
                if(f) {
                    var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
                    setter.call(f, arguments[0]);
                    f.dispatchEvent(new Event('input',{bubbles:true}));
                    f.dispatchEvent(new Event('change',{bubbles:true}));
                    break;
                }
            }
        """, sms_code)
        driver.execute_script("document.querySelector('button[type=submit],button.btn-primary').click();")
        time.sleep(5)

        if "Login" not in driver.current_url:
            print("LOGIN SUCCESS! URL:", driver.current_url)
            driver.get("https://www.interactivebrokers.com/portal/#/settings/user?selectedTab=docs")
            time.sleep(3)
            print("Document upload page ready in browser.")
            return driver
        else:
            print("Login failed. Body:", driver.find_element(By.TAG_NAME,"body").text[:300])
            driver.quit()
            return None
    except Exception as e:
        import traceback; traceback.print_exc()
        driver.quit()
        return None


if __name__ == "__main__":
    result = ibkr_login_with_sms()
    if result:
        input("Browser open at document upload page. Press Enter to close.")
        result.quit()
