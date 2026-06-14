"""
Script to test F1TV auth cookies for radio downloads.

HOW TO GET THE COOKIES:
1. Open Chrome/Edge, go to https://www.formula1.com (must be logged into F1TV Pro)
2. Press F12 → Application → Cookies → https://www.formula1.com
3. Copy values for:
   - login-session
   - entitlement_token

Run: python get_cf_cookies.py
"""
import os
import requests


def test_cookies(login_session: str, entitlement_token: str, token: str) -> bool:
    url = "https://livetiming.formula1.com/TeamRadio/COL_43_20260614_153044.mp3"
    
    combos = [
        ("login-session only", {"login-session": login_session}, {}),
        ("entitlement_token only", {"entitlement_token": entitlement_token}, {}),
        ("both cookies", {"login-session": login_session, "entitlement_token": entitlement_token}, {}),
        ("both + bearer", {"login-session": login_session, "entitlement_token": entitlement_token}, {"Authorization": f"Bearer {token}"}),
        ("entitlement as bearer", {}, {"Authorization": f"Bearer {entitlement_token}"}),
    ]
    
    for name, cookies, headers in combos:
        r = requests.get(url, cookies=cookies, headers=headers, timeout=10)
        print(f"  {name}: HTTP {r.status_code} ({len(r.content)} bytes)")
        if r.status_code == 200:
            print(f"  ✅ SUCCESS with: {name}")
            return name, cookies, headers
    return None


def save_to_env(key: str, value: str):
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    with open(env_path, "rb") as f:
        content = f.read().decode("utf-8", errors="ignore")
    lines = [l for l in content.splitlines() if not l.startswith(key + "=")]
    lines.append(f"{key}={value}")
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    with open(env_path, "rb") as f:
        env_content = f.read().decode("utf-8", errors="ignore")
    token = next((l.split("=", 1)[1].strip() for l in env_content.split("\n") if "F1_SUBSCRIPTION_TOKEN" in l), "")

    print("Enter cookies from F1TV (F12 → Application → Cookies → formula1.com):\n")
    login_session = input("login-session value: ").strip()
    entitlement_token = input("entitlement_token value: ").strip()

    print("\nTesting combinations...")
    result = test_cookies(login_session, entitlement_token, token)

    if result:
        name, cookies, headers = result
        print(f"\n✅ Works! Saving to .env...")
        for k, v in cookies.items():
            env_key = k.upper().replace("-", "_")
            save_to_env(f"F1_COOKIE_{env_key}", v)
        print("Done! Now run: scp -i ~/.ssh/f1-bot-key.pem .env ec2-user@54.204.247.143:/home/ec2-user/f1-news-bot/.env")
        print("Then: ssh -i ~/.ssh/f1-bot-key.pem ec2-user@54.204.247.143 'sudo systemctl restart f1-telemetry-bot'")
    else:
        print("\n❌ None worked. F1TV Pro radio files may only be accessible via the browser with active session.")
        print("Try: open the F1TV player and watch something first, then copy cookies again.")

import sys
import os
import requests


def test_cookies(policy: str, signature: str, key_pair_id: str, token: str) -> bool:
    url = "https://livetiming.formula1.com/TeamRadio/COL_43_20260614_153044.mp3"
    cookies = {
        "CloudFront-Policy": policy,
        "CloudFront-Signature": signature,
        "CloudFront-Key-Pair-Id": key_pair_id,
    }
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, cookies=cookies, headers=headers, timeout=15)
    print(f"Test result: {r.status_code} ({len(r.content)} bytes)")
    return r.status_code == 200


def try_auto_extract(token: str) -> dict | None:
    """Try to get CF cookies via F1TV API."""
    import requests
    s = requests.Session()
    
    # Try the F1TV subscription page which should set CloudFront cookies
    endpoints = [
        ("GET", "https://f1tv.formula1.com/2.0/R/ENG/BIG_SCREEN_HLS/ALL/USER/SUBSCRIPTION"),
        ("GET", "https://api.formula1.com/v2/account/subscriber"),
        ("POST", "https://f1tv.formula1.com/2.0/R/ENG/BIG_SCREEN_HLS/ALL/USER/AUTHENTICATE"),
    ]
    
    headers = {
        "Authorization": f"Bearer {token}",
        "apiKey": "fCUCjWrKPu9ylJwRAv8BpGLEgiAuThx7",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
        "Origin": "https://www.formula1.com",
        "Referer": "https://www.formula1.com/",
    }
    
    for method, url in endpoints:
        try:
            r = s.request(method, url, headers=headers, timeout=10, allow_redirects=True)
            cf = {k: v for k, v in s.cookies.items() if "CloudFront" in k}
            if cf:
                print(f"Got CF cookies from {url.split('/')[-1]}")
                return cf
            print(f"  {url.split('/')[-1]}: {r.status_code} — no CF cookies")
        except Exception as e:
            print(f"  {url.split('/')[-1]}: ERROR {e}")
    return None


def save_to_env(policy: str, signature: str, key_pair_id: str):
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    with open(env_path, "rb") as f:
        content = f.read().decode("utf-8", errors="ignore")
    
    lines = content.splitlines()
    new_lines = [l for l in lines if not any(k in l for k in ["CF_POLICY=", "CF_SIGNATURE=", "CF_KEY_PAIR_ID="])]
    new_lines += [
        f"CF_POLICY={policy}",
        f"CF_SIGNATURE={signature}",
        f"CF_KEY_PAIR_ID={key_pair_id}",
    ]
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(new_lines) + "\n")
    print("Saved to .env!")


if __name__ == "__main__":
    # Load token from .env
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    with open(env_path, "rb") as f:
        env_content = f.read().decode("utf-8", errors="ignore")
    token = next((l.split("=", 1)[1].strip() for l in env_content.split("\n") if "F1_SUBSCRIPTION_TOKEN" in l), "")
    
    if "--auto" in sys.argv:
        print("Trying automatic extraction...")
        cf = try_auto_extract(token)
        if cf:
            policy = cf.get("CloudFront-Policy", "")
            signature = cf.get("CloudFront-Signature", "")
            key_pair_id = cf.get("CloudFront-Key-Pair-Id", "")
        else:
            print("Auto extraction failed. Enter cookies manually.")
            sys.exit(1)
    else:
        print("Enter CloudFront cookies from F1TV (F12 → Application → Cookies → formula1.com):\n")
        policy = input("CloudFront-Policy: ").strip()
        signature = input("CloudFront-Signature: ").strip()
        key_pair_id = input("CloudFront-Key-Pair-Id: ").strip()
    
    if not all([policy, signature, key_pair_id]):
        print("Missing cookies!")
        sys.exit(1)
    
    print("\nTesting cookies...")
    ok = test_cookies(policy, signature, key_pair_id, token)
    
    if ok:
        save_to_env(policy, signature, key_pair_id)
        print("\nCookies work! Now update the bot and redeploy.")
    else:
        print("\nCookies didn't work. Make sure you're logged into F1TV Pro and copy fresh cookies.")
