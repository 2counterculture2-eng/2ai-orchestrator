"""
ibkr_sms_auto_login.py v3
Flow: IBKR login -> LINE notification "send SMS code" -> user replies 6 digits ->
      Railway /api/sms-code -> auto-complete login -> document upload page opens.
"""
from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
import time, urllib.request, urllib.parse, json, datetime

IBKR_USER = "takuma2ai9"
IBKR_PASS = "Ibkr2AI2025!"
IBKR_LOGIN_URL = "https://www.interactivebrokers.com/sso/Login?action=DOC"
RAILWAY_BASE = "https://orchestrator-production-61d8.up.railway.app"
LINE_USER_ID = "Ud3be14241e193a4a7bf80a1b10a004c0"
LINE_TOKEN_ENV = "LINE_CHANNEL_ACCESS_TOKEN"


def _send_line(msg: str):
    import os
    token = os.getenv(LINE_TOKEN_ENV, "")
    if not token:
        # Read from Railway env via API
        try:
            r = urllib.request.urlopen(RAILWAY_BASE + "/debug/line-user-id", timeout=5)
        except Exception:
            pass
        return
    data = json.dumps({"to": LINE_USER_ID, "messages": [{"type": "text", "text": msg}]}).encode()
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push",
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"LINE send error: {e}")


def _notify_via_railway(msg: str):
    """Use Railway debug endpoint to trigger LINE notification."""
    try:
        data = json.dumps({"message": msg}).encode()
        req = urllib.request.Request(
            RAILWAY_BASE + "/api/notify-line",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def wait_for_sms_code(timeout=180) -> str:
    """Poll Railway endpoint until a fresh SMS code arrives (within last 3 min)."""
    print(f"Waiting for IBKR SMS code via LINE reply (max {timeout}s)...")
    deadline = time.time() + timeout
    # Record current time to only accept new codes
    sent_at = datetime.datetime.now(datetime.timezone.utc)

    while time.time() < deadline:
        try:
            r = urllib.request.urlopen(RAILWAY_BASE + "/api/sms-code", timeout=5)
            data = json.loads(r.read())
            code = data.get("code", "")
            ts_str = data.get("time", "")
            if code and ts_str:
                ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts > sent_at:
                    print(f"Code received: {code}")
                    return code
        except Exception:
            pass
        time.sleep(3)
    return ""


def ibkr_login_with_sms_via_line():
    opts = Options()
    opts.add_argument("--no-sandbox")
    driver = webdriver.Edge(options=opts)
    try:
        # Step 1: Open IBKR login
        driver.get(IBKR_LOGIN_URL)
        time.sleep(3)
        driver.find_element(By.NAME, "username").send_keys(IBKR_USER)
        driver.find_element(By.NAME, "password").send_keys(IBKR_PASS)
        driver.find_element(By.XPATH, '//button[@type="submit"]').click()
        time.sleep(4)
        print("Credentials submitted. Notifying via LINE...")

        # Step 2: Notify LINE to send SMS code
        _notify_via_railway("IBKRからSMSコードが届いたら、このLINEに6桁の数字だけ送ってください。")

        # Step 3: Wait for code from Railway
        sms_code = wait_for_sms_code(timeout=180)
        if not sms_code:
            print("ERROR: No code received within 3 minutes")
            driver.quit()
            return None

        # Step 4: Enter code in IBKR form
        driver.execute_script("""
            var names = ['temp-response','bronze-response','silver-response'];
            for(var n of names){
                var f=document.querySelector('[name='+n+']');
                if(f){
                    var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
                    s.call(f,arguments[0]);
                    f.dispatchEvent(new Event('input',{bubbles:true}));
                    f.dispatchEvent(new Event('change',{bubbles:true}));
                    break;
                }
            }
        """, sms_code)
        driver.execute_script("document.querySelector('button[type=submit],button.btn-primary').click();")
        time.sleep(5)

        url = driver.current_url
        if "Login" not in url:
            print("LOGIN SUCCESS! Opening document upload page...")
            _notify_via_railway("IBKRログイン成功！書類アップロードページを開きました。")
            driver.get("https://www.interactivebrokers.com/portal/#/settings/user?selectedTab=docs")
            time.sleep(3)
            return driver
        else:
            body = driver.find_element(By.TAG_NAME, "body").text[:200]
            print("Login failed:", body)
            driver.quit()
            return None

    except Exception as e:
        import traceback; traceback.print_exc()
        driver.quit()
        return None


if __name__ == "__main__":
    result = ibkr_login_with_sms_via_line()
    if result:
        input("Browser open at document upload page. Press Enter to close.")
        result.quit()
