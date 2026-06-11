import os
import json
import time
import base64
import struct
import sys
import re
import ctypes
import hashlib
from datetime import datetime
from urllib.parse import urljoin, urlparse, parse_qs
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from typing import List, Optional, Dict, Any
from pydantic import BaseModel
import uvicorn
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from google import genai
from google.genai import types

# Configure stdout/stderr to use UTF-8 to prevent encoding errors on non-UTF-8 terminals
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

def get_png_dimensions(png_bytes):
    try:
        if len(png_bytes) >= 24 and png_bytes[:8] == b'\x89PNG\r\n\x1a\n':
            width, height = struct.unpack('>II', png_bytes[16:24])
            return width, height
    except Exception as e:
        print(f"Error parsing PNG dimensions: {e}")
    return None, None

def scale_coordinates(x, y, img_w, img_h, page):
    if not img_w or not img_h or x is None or y is None:
        return x, y
    viewport = page.viewport_size or {"width": 1280, "height": 800}
    view_w = viewport["width"]
    view_h = viewport["height"]
    scale_x = view_w / img_w
    scale_y = view_h / img_h
    return int(x * scale_x), int(y * scale_y)

# Load environment variables manually from project root (.env and .env.local)
def load_env_manually():
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for env_file in [".env", ".env.local"]:
        env_path = os.path.join(base_dir, env_file)
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip('"').strip("'")
                            os.environ[k] = v
            except Exception as e:
                print(f"Failed to parse {env_file}: {e}")

load_env_manually()

# Initialize Gemini Client for vision analysis
api_key = os.getenv("GEMINI_API_KEY")
client = None
if api_key:
    try:
        client = genai.Client(api_key=api_key)
        print("Gemini GenAI client initialized successfully for Playwright vision loops!")
    except Exception as e:
        print(f"Failed to initialize Gemini client: {e}")

def email_service_url(path):
    base_url = (os.getenv("EMAIL_URL") or "").rstrip("/")
    if not base_url:
        email_server_port = int(os.getenv("EMAIL_SERVER_PORT", 8005))
        base_url = f"http://localhost:{email_server_port}"
    return f"{base_url}{path}"

def vision_login_linkedin(page, username, password):
    if not client:
        print("[Vision Login] Gemini client not configured, skipping vision login.")
        return False
        
    try:
        print("[Vision Login] Initiating vision-based login...")
        # Wait dynamically up to 10s for the username input to become visible
        try:
            page.wait_for_selector('input#username:visible, input[name="session_key"]:visible, input[type="email"]:visible', state="visible", timeout=10000)
            print("[Vision Login] Login form detected. Waiting an additional 1s for layout to settle...")
            page.wait_for_timeout(1000)
        except Exception as wait_err:
            print(f"[Vision Login Warning] Timeout waiting for login form elements: {wait_err}. Proceeding with screenshot...")
        
        # 1. Take a screenshot of the login page
        screenshot_bytes = page.screenshot(type="png")
        img_w, img_h = get_png_dimensions(screenshot_bytes)
        print(f"[Vision Login] Physical screenshot resolution: {img_w}x{img_h}")
        
        image_part = types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png")
        
        prompt = f"""
        You are the visual coordinator of a browser automation agent.
        You are looking at the LinkedIn Login page.
        Your task is to locate:
        1. The username/email input field.
        2. The password input field.
        3. The "Sign in" or login submission button.
        
        Note: The screenshot has a physical resolution of {img_w}x{img_h} pixels.
        You MUST return coordinates relative to this {img_w}x{img_h} coordinate space (0 to {img_w} for X, 0 to {img_h} for Y).
        
        Return ONLY a valid JSON object matching this schema (no markdown, just raw JSON):
        {{
          "username_x": integer,
          "username_y": integer,
          "password_x": integer,
          "password_y": integer,
          "submit_x": integer,
          "submit_y": integer
        }}
        """
        
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=[image_part, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0
            )
        )
        
        coords_text = response.text.strip()
        if coords_text.startswith("```json"):
            coords_text = coords_text.replace("```json", "").replace("```", "").strip()
        elif coords_text.startswith("```"):
            coords_text = coords_text.replace("```", "").strip()
            
        coords = json.loads(coords_text)
        
        # Validate coordinates: if model returned (-1, -1) or None, skip vision login
        for key in ["username_x", "username_y", "password_x", "password_y", "submit_x", "submit_y"]:
            val = coords.get(key)
            if val is None or val < 0:
                print(f"[Vision Login] Invalid coordinate {key}={val}. Skipping vision login and falling back to selectors.")
                return False
        
        # Scale coordinates
        ux, uy = scale_coordinates(coords.get("username_x"), coords.get("username_y"), img_w, img_h, page)
        px, py = scale_coordinates(coords.get("password_x"), coords.get("password_y"), img_w, img_h, page)
        sx, sy = scale_coordinates(coords.get("submit_x"), coords.get("submit_y"), img_w, img_h, page)
        
        # Click and fill username
        print(f"[Vision Login] Clicking username input at CSS ({ux}, {uy}) [raw: {coords.get('username_x')}, {coords.get('username_y')}]")
        page.mouse.click(ux, uy)
        page.wait_for_timeout(500)
        # Clear field and type
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.keyboard.type(username)
        page.wait_for_timeout(500)
        
        # Click and fill password
        print(f"[Vision Login] Clicking password input at CSS ({px}, {py}) [raw: {coords.get('password_x')}, {coords.get('password_y')}]")
        page.mouse.click(px, py)
        page.wait_for_timeout(500)
        # Clear field and type
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.keyboard.type(password)
        page.wait_for_timeout(500)
        
        # Click submit button
        print(f"[Vision Login] Clicking Sign In button at CSS ({sx}, {sy}) [raw: {coords.get('submit_x')}, {coords.get('submit_y')}]")
        page.mouse.click(sx, sy)
        page.wait_for_timeout(4000)
        
        return True
    except Exception as e:
        print(f"[Vision Login Error] {e}")
        return False

def vision_solve_challenge_linkedin(page, username):
    if not client:
        return False
        
    try:
        print("[Vision Challenge] Initiating vision-based challenge solver...")
        # Wait dynamically up to 10s for the challenge input to become visible
        try:
            pin_selector = 'input#input-pin:visible, input#email-pin:visible, input[name="pin"]:visible, input[autocomplete="one-time-code"]:visible'
            page.wait_for_selector(pin_selector, state="visible", timeout=10000)
            print("[Vision Challenge] Challenge form detected. Waiting an additional 1s for layout to settle...")
            page.wait_for_timeout(1000)
        except Exception as wait_err:
            print(f"[Vision Challenge Warning] Timeout waiting for challenge elements: {wait_err}. Proceeding with screenshot...")
        
        # Take a screenshot of the challenge page
        screenshot_bytes = page.screenshot(type="png")
        img_w, img_h = get_png_dimensions(screenshot_bytes)
        print(f"[Vision Challenge] Physical screenshot resolution: {img_w}x{img_h}")
        
        image_part = types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png")
        
        prompt = f"""
        You are looking at a LinkedIn verification challenge or security pin checkpoint page.
        Locate:
        1. The verification code input field (where a 6-digit pin is entered).
        2. The submit/verify button.
        
        Note: The screenshot has a physical resolution of {img_w}x{img_h} pixels.
        You MUST return coordinates relative to this {img_w}x{img_h} coordinate space (0 to {img_w} for X, 0 to {img_h} for Y).
        
        Return ONLY a JSON object matching this schema (no markdown, just raw JSON):
        {{
          "pin_x": integer or null,
          "pin_y": integer or null,
          "submit_x": integer or null,
          "submit_y": integer or null
        }}
        """
        
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=[image_part, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0
            )
        )
        
        coords_text = response.text.strip()
        if coords_text.startswith("```json"):
            coords_text = coords_text.replace("```json", "").replace("```", "").strip()
        elif coords_text.startswith("```"):
            coords_text = coords_text.replace("```", "").strip()
            
        coords = json.loads(coords_text)
        
        raw_pin_x = coords.get("pin_x")
        raw_pin_y = coords.get("pin_y")
        raw_submit_x = coords.get("submit_x")
        raw_submit_y = coords.get("submit_y")
        
        if raw_pin_x is None or raw_pin_x < 0 or raw_pin_y is None or raw_pin_y < 0 or raw_submit_x is None or raw_submit_x < 0 or raw_submit_y is None or raw_submit_y < 0:
            print("[Vision Challenge] Invalid challenge coordinates returned. Skipping vision challenge solver.")
            return False
            
        pin_x, pin_y = scale_coordinates(raw_pin_x, raw_pin_y, img_w, img_h, page)
        submit_x, submit_y = scale_coordinates(raw_submit_x, raw_submit_y, img_w, img_h, page)
        
        print(f"[Vision Challenge] Verification PIN field found at CSS ({pin_x}, {pin_y}). Fetching OTP from email...")
        # Wait for email to arrive
        page.wait_for_timeout(10000)
        
        # Fetch OTP
        email_otp_url = email_service_url("/email/otp")
        import requests
        resp = requests.post(email_otp_url, json={
            "email_address": username,
            "sender_filter": "linkedin.com"
        }, timeout=15)
        
        if resp.status_code == 200:
            otp_data = resp.json()
            otp_code = otp_data.get("otp_code")
            if otp_code:
                print(f"[Vision Challenge] Got OTP code '{otp_code}'. Filling PIN field...")
                page.mouse.click(pin_x, pin_y)
                page.wait_for_timeout(500)
                page.keyboard.type(otp_code)
                page.wait_for_timeout(500)
                
                print(f"[Vision Challenge] Clicking verify button at CSS ({submit_x}, {submit_y})...")
                page.mouse.click(submit_x, submit_y)
                page.wait_for_timeout(5000)
                return True
        else:
            print(f"[Vision Challenge] Email OTP fetch failed: {resp.text}")
        return False
    except Exception as e:
        print(f"[Vision Challenge Error] {e}")
        return False


def chromium_launch_options(headless=True, extra_args=None):
    args = ["--disable-popup-blocking", "--no-sandbox", "--disable-dev-shm-usage"]
    if extra_args:
        args.extend(extra_args)
    options = {
        "headless": headless,
        "args": args,
    }
    executable_path = (
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or os.getenv("CHROMIUM_EXECUTABLE_PATH")
        or ""
    ).strip()
    if executable_path:
        options["executable_path"] = executable_path
    return options


def launch_browser(p, headless=True, locale="en-US", timezone="America/New_York"):
    # Check if user wants to use a persistent Chrome Profile to share login session
    use_persistent = os.getenv("PLAYWRIGHT_USE_PERSISTENT_PROFILE", "false").lower() == "true"
    
    # Default to a project-local data directory to prevent locking user's main browser profile
    default_local_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "linkedin_user_data")
    profile_dir = os.getenv("PLAYWRIGHT_CHROME_PROFILE_DIR", default_local_dir)
    profile_name = os.getenv("PLAYWRIGHT_CHROME_PROFILE_NAME", "")
    
    if use_persistent:
        print(f"Attempting to launch with persistent user data directory: {profile_dir}...")
        try:
            os.makedirs(profile_dir, exist_ok=True)
            args = ["--disable-popup-blocking"]
            if profile_name:
                args.append(f"--profile-directory={profile_name}")

            launch_options = chromium_launch_options(headless=headless, extra_args=args)
            launch_options.pop("headless", None)
            launch_options.pop("args", None)
            # Use persistent context to inherit logins.
            context = p.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=headless,
                viewport={"width": 1280, "height": 800},
                locale=locale,
                timezone_id=timezone,
                args=chromium_launch_options(headless=headless, extra_args=args)["args"],
                **launch_options
            )
            return context, True # Returns context and a flag indicating it is persistent
        except Exception as e:
            print(f"Failed to launch persistent context: {e}. Falling back to standard launch...")

    # Standard launch fallback
    executable_path = (os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or os.getenv("CHROMIUM_EXECUTABLE_PATH") or "").strip()
    if executable_path:
        try:
            print(f"Attempting to launch browser with executable_path={executable_path}...")
            browser = p.chromium.launch(**chromium_launch_options(headless=headless))
            return browser, False
        except Exception as e:
            print(f"Configured Chromium launch failed: {e}. Trying browser channels...")

    try:
        print("Attempting to launch browser with local Google Chrome channel...")
        browser = p.chromium.launch(headless=headless, channel="chrome")
        return browser, False
    except Exception as e:
        print(f"Local Google Chrome launch failed: {e}. Trying Microsoft Edge...")
    
    try:
        print("Attempting to launch browser with local Microsoft Edge channel...")
        browser = p.chromium.launch(headless=headless, channel="msedge")
        return browser, False
    except Exception as e:
        print(f"Local Microsoft Edge launch failed: {e}. Falling back to default Chromium...")
        
    return p.chromium.launch(headless=headless), False

def dismiss_popups(page):
    try:
        body_text = ""
        try:
            body_text = page_body_text(page, timeout=800).lower()
        except Exception:
            body_text = ""
        if (
            "start your application" in body_text
            and (
                "autofill with resume" in body_text
                or "apply manually" in body_text
                or "use my last application" in body_text
            )
        ):
            return
        dismiss_selectors = [
            'button.modal__dismiss',
            'button[aria-label="Dismiss"]',
            'button[aria-label="Close"]',
            'button.modal-close',
            'button.artdeco-modal__dismiss',
            'button[data-action="close"]',
            '.contextual-sign-in-modal__join-modal-dismiss-btn',
            '.modal__dismiss-btn',
            '.cookie-banner__dismiss'
        ]
        for selector in dismiss_selectors:
            # Check if element is visible and click it
            el = page.locator(selector)
            if el.count() > 0 and el.first.is_visible():
                print(f"[Playwright] Detected popup/modal. Dismissing with selector: {selector}")
                el.first.click()
                page.wait_for_timeout(1000)
    except Exception as e:
        print(f"[Playwright Warning] Error while dismissing popups: {e}")

def sanitize_cookies(cookies: list) -> list:
    sanitized = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        if "name" not in cookie or "value" not in cookie:
            continue
        
        name = str(cookie["name"])
        value = str(cookie["value"])
        
        # Ensure JSESSIONID is double-quoted to satisfy LinkedIn's CSRF check
        if name == "JSESSIONID" and not (value.startswith('"') and value.endswith('"')):
            value = f'"{value}"'
            
        c = {
            "name": name,
            "value": value,
        }
        
        # Keep original domains (like .www.linkedin.com) to prevent redirect loops
        if "domain" in cookie and cookie["domain"]:
            c["domain"] = str(cookie["domain"])
        else:
            c["domain"] = ".linkedin.com"
            
        if "path" in cookie:
            c["path"] = str(cookie["path"])
        else:
            c["path"] = "/"
            
        if "expires" in cookie and cookie["expires"] is not None:
            try:
                c["expires"] = float(cookie["expires"])
            except (ValueError, TypeError):
                pass
        if "httpOnly" in cookie:
            c["httpOnly"] = bool(cookie["httpOnly"])
        if "secure" in cookie:
            c["secure"] = bool(cookie["secure"])
        if "sameSite" in cookie:
            ss = str(cookie["sameSite"])
            if ss.lower() in ["strict", "lax", "none"]:
                c["sameSite"] = ss.capitalize()
            elif ss.lower() == "no_restriction":
                c["sameSite"] = "None"
        sanitized.append(c)
    return sanitized

def extract_timezone_and_locale(cookies: list):
    timezone = "America/New_York"  # Default fallback
    locale = "en-US"              # Default fallback
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = cookie.get("name")
        val = cookie.get("value")
        if name == "timezone" and val:
            timezone = str(val)
        elif name == "lang" and val:
            val_str = str(val)
            if "lang=" in val_str:
                try:
                    locale = val_str.split("lang=")[1].split("&")[0]
                except Exception:
                    pass
            elif len(val_str) < 10:
                locale = val_str
    return timezone, locale

def fill_first_available(page, selectors, value):
    if value is None or value == "":
        return False

    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() == 0:
                continue
            if not locator.is_visible(timeout=1000):
                continue
            locator.fill(str(value), timeout=3000)
            print(f"[Structured Fill] Filled selector: {selector}")
            return True
        except Exception:
            continue
    return False

def try_structured_form_fill(page, resume_path, user_data, allow_submit=False):
    filled = {}
    fields = {
        "first_name": [
            'input#first_name',
            'input[name="first_name"]',
            'input[name*="first" i]',
            'input[id*="first" i]',
            'input[autocomplete="given-name"]',
            'input[placeholder*="John" i]'
        ],
        "last_name": [
            'input#last_name',
            'input[name="last_name"]',
            'input[name*="last" i]',
            'input[id*="last" i]',
            'input[autocomplete="family-name"]',
            'input[placeholder*="Doe" i]'
        ],
        "email": [
            'input#email',
            'input[type="email"]',
            'input[name*="email" i]',
            'input[id*="email" i]',
            'input[autocomplete="email"]'
        ],
        "phone": [
            'input#phone',
            'input[type="tel"]',
            'input[name*="phone" i]',
            'input[id*="phone" i]',
            'input[autocomplete="tel"]',
            'input[placeholder*="555" i]'
        ],
    }

    for key, selectors in fields.items():
        filled[key] = fill_first_available(page, selectors, user_data.get(key))

    uploaded_resume = False
    if resume_path and os.path.exists(resume_path):
        try:
            file_input = page.locator('input[type="file"]').first
            if file_input.count() > 0:
                file_input.set_input_files(resume_path, timeout=3000)
                uploaded_resume = True
                print(f"[Structured Fill] Uploaded resume: {resume_path}")
        except Exception as upload_err:
            print(f"[Structured Fill] Resume upload skipped: {upload_err}")
    else:
        print(f"[Structured Fill] Resume path does not exist: {resume_path}")

    print(f"[Structured Fill] Summary: fields={filled}, uploaded_resume={uploaded_resume}")

    if not allow_submit:
        return False

    try:
        submit_selectors = [
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("Submit")',
            'button:has-text("Apply")'
        ]
        for selector in submit_selectors:
            button = page.locator(selector).first
            if button.count() > 0 and button.is_visible(timeout=1000):
                button.click(timeout=3000)
                page.wait_for_timeout(1500)
                body_text = page.locator("body").inner_text(timeout=3000)
                return "Application Submitted Successfully" in body_text or "Success" in body_text
    except Exception as submit_err:
        print(f"[Structured Fill] Submit skipped/failed: {submit_err}")

    return False

FINAL_SUBMIT_RE = re.compile(
    r"\b(submit application|send application|complete application|finish application|final submit)\b",
    re.IGNORECASE
)

HIGH_RISK_FIELD_RE = re.compile(
    r"(sponsor|visa|work authorization|authorized to work|citizen|eeo|disability|veteran|gender|race|ethnicity|"
    r"criminal|background check|salary|compensation|relocat|date of birth|age)",
    re.IGNORECASE
)

MEDIUM_RISK_FIELD_RE = re.compile(
    r"(cover letter|why|experience|years|education|school|degree|address|resume|cv|upload|portfolio)",
    re.IGNORECASE
)

SAFE_DISCOVERY_CHECKBOX_RE = re.compile(
    r"\b(use my profile|build resume|resume builder|import profile|use existing profile|copy from profile)\b",
    re.IGNORECASE
)

def load_default_user_data():
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "scheduler_config.json")
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, "r", encoding="utf-8-sig") as f:
            config_data = json.load(f)
        return config_data.get("user_data", {}) or {}
    except Exception as err:
        print(f"[Access Gate] Failed to read default user data: {err}")
        return {}

def get_application_password(req, account_record=None):
    if req.application_password:
        return req.application_password, "request"
    stored = decrypt_secret((account_record or {}).get("password"))
    if stored:
        return stored, "registry"
    env_password = os.getenv("ATS_ACCOUNT_PASSWORD") or os.getenv("APPLICATION_ACCOUNT_PASSWORD")
    if env_password:
        return env_password, "env"
    return "ivantestest", "default"

def adapt_password_to_visible_policy(page, password, enabled=True):
    if not enabled or not password:
        return password, False
    text = page_body_text(page).lower()
    ats = infer_ats(page.url)
    adjusted = password
    changed = False
    special_chars = r"{}[].<>:;?\"/|~!@#$%^&*()_-+="

    if (("special" in text or ats == "brassring") and not any(ch in special_chars for ch in adjusted)):
        adjusted += "!"
        changed = True
    if ("number" in text or "numeric" in text or "digit" in text) and not any(ch.isdigit() for ch in adjusted):
        adjusted += "1"
        changed = True
    if ("uppercase" in text or "upper case" in text or "upper & lower" in text or "upper and lower" in text) and not any(ch.isupper() for ch in adjusted):
        adjusted = "I" + adjusted
        changed = True
    if ("lowercase" in text or "lower case" in text) and not any(ch.islower() for ch in adjusted):
        adjusted += "a"
        changed = True
    if len(adjusted) < 8:
        adjusted = adjusted + ("1!" * 4)
        adjusted = adjusted[:8]
        changed = True
    return adjusted, changed

def get_security_answers(req, user_data):
    if req.security_answers:
        answers = [answer for answer in req.security_answers if answer]
    else:
        answers = [
            os.getenv("ATS_SECURITY_ANSWER_1"),
            os.getenv("ATS_SECURITY_ANSWER_2"),
            os.getenv("ATS_SECURITY_ANSWER_3")
        ]
        answers = [answer for answer in answers if answer]

    if len(answers) >= 3:
        return answers[:3]

    first = re.sub(r"\W+", "", user_data.get("first_name", "") or "Candidate")
    last = re.sub(r"\W+", "", user_data.get("last_name", "") or "Profile")
    fallback_base = f"{first}{last}" or "CandidateProfile"
    while len(answers) < 3:
        answers.append(f"{fallback_base}{len(answers) + 1}!")
    return answers[:3]

def registry_data_dir():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

def account_registry_path():
    return os.path.join(registry_data_dir(), "apply_account_registry.json")

class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]

def dpapi_protect(data):
    if os.name != "nt":
        raise RuntimeError("DPAPI secret storage is only available on Windows")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_buffer = ctypes.create_string_buffer(data)
    in_blob = DATA_BLOB(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
    out_blob = DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)

def dpapi_unprotect(data):
    if os.name != "nt":
        raise RuntimeError("DPAPI secret storage is only available on Windows")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_buffer = ctypes.create_string_buffer(data)
    in_blob = DATA_BLOB(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
    out_blob = DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)

def encrypt_secret(secret):
    if not secret:
        return None
    if os.name == "nt":
        blob = dpapi_protect(secret.encode("utf-8"))
        return {
            "scheme": "windows-dpapi",
            "blob": base64.b64encode(blob).decode("ascii")
        }
    if os.getenv("ALLOW_PLAINTEXT_ATS_PASSWORD_STORAGE", "false").lower() == "true":
        return {
            "scheme": "plaintext-dev-only",
            "value": secret
        }
    return None

def decrypt_secret(secret_record):
    if not isinstance(secret_record, dict):
        return None
    scheme = secret_record.get("scheme")
    try:
        if scheme == "windows-dpapi":
            encrypted = base64.b64decode(secret_record.get("blob", ""))
            return dpapi_unprotect(encrypted).decode("utf-8")
        if scheme == "plaintext-dev-only" and os.getenv("ALLOW_PLAINTEXT_ATS_PASSWORD_STORAGE", "false").lower() == "true":
            return secret_record.get("value")
    except Exception as err:
        print(f"[Access Gate] Stored ATS password could not be decrypted: {err}")
    return None

def load_account_registry():
    path = account_registry_path()
    if not os.path.exists(path):
        return {"version": 1, "accounts": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"version": 1, "accounts": {}}
        data.setdefault("version", 1)
        data.setdefault("accounts", {})
        return data
    except Exception as err:
        print(f"[Access Gate] Failed to read account registry: {err}")
        return {"version": 1, "accounts": {}}

def save_account_registry(registry):
    os.makedirs(registry_data_dir(), exist_ok=True)
    path = account_registry_path()
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)

def infer_account_tenant(url):
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    query = parse_qs(parsed.query or "")
    if "brassring" in host:
        partner = (query.get("partnerid") or [""])[0]
        site = (query.get("siteid") or [""])[0]
        return f"partnerid={partner};siteid={site}" if partner or site else host
    if "myworkdayjobs" in host:
        return host.split(".")[0]
    if "greenhouse" in host or "ashby" in host or "lever.co" in host:
        return host
    return host

def build_account_key(url, email_address):
    ats = infer_ats(url)
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    tenant = infer_account_tenant(url)
    email_key = (email_address or "").strip().lower()
    return f"{ats}|{host}|{tenant}|{email_key}", {
        "ats": ats,
        "host": host,
        "tenant": tenant,
        "email": email_key
    }

def get_registry_account(key):
    return load_account_registry().get("accounts", {}).get(key, {})

def remember_apply_account(key, meta, password=None, event="observed"):
    registry = load_account_registry()
    accounts = registry.setdefault("accounts", {})
    existing = accounts.get(key, {})
    now = datetime.now().isoformat()
    record = {
        **existing,
        **meta,
        "updated_at": now,
        "last_event": event,
    }
    if event in {"created", "login"}:
        record["account_created"] = True
        if not record.get("created_at"):
            record["created_at"] = now
    elif event == "exists_warning":
        record["account_exists"] = True
        record["account_created"] = True
        if not record.get("created_at"):
            record["created_at"] = existing.get("created_at") or now
    else:
        record["account_created"] = bool(existing.get("account_created"))
    if event in {"login", "created"}:
        record["last_login_at"] = now
    if event == "form_access":
        record["last_form_access_at"] = now
    if event == "guest_apply":
        record["guest_supported"] = True
        record["last_guest_apply_at"] = now
    if event == "guest_email_sent":
        record["guest_supported"] = True
        record["guest_email_link_required"] = True
        record["last_email_link_sent_at"] = now
    if event == "email_verification_solved":
        record["last_email_verification_at"] = now
    if password:
        secret_record = encrypt_secret(password)
        if secret_record:
            record["password"] = secret_record
            record["password_updated_at"] = now
            record["password_storage"] = secret_record.get("scheme")
    accounts[key] = record
    save_account_registry(registry)
    return record

def sanitized_account_status(key, record):
    if not record:
        return {
            "registry_key": key,
            "account_created": False,
            "account_exists": False,
            "guest_supported": False,
            "guest_email_link_required": False,
            "password_saved": False
        }
    return {
        "registry_key": key,
        "ats": record.get("ats"),
        "host": record.get("host"),
        "tenant": record.get("tenant"),
        "email": record.get("email"),
        "account_created": bool(record.get("account_created")),
        "account_exists": bool(record.get("account_exists")),
        "guest_supported": bool(record.get("guest_supported")),
        "guest_email_link_required": bool(record.get("guest_email_link_required")),
        "password_saved": bool(record.get("password")),
        "password_storage": record.get("password_storage"),
        "created_at": record.get("created_at"),
        "last_login_at": record.get("last_login_at"),
        "last_form_access_at": record.get("last_form_access_at"),
        "last_guest_apply_at": record.get("last_guest_apply_at"),
        "last_email_link_sent_at": record.get("last_email_link_sent_at"),
        "last_email_verification_at": record.get("last_email_verification_at"),
        "last_event": record.get("last_event")
    }

def infer_ats(url):
    host = (urlparse(url or "").hostname or "").lower()
    if "brassring" in host:
        return "brassring"
    if "greenhouse" in host:
        return "greenhouse"
    if "ashbyhq" in host or "ashby" in host:
        return "ashby"
    if "lever.co" in host:
        return "lever"
    if "myworkdayjobs" in host or "workday" in host:
        return "workday"
    if "icims" in host:
        return "icims"
    if "smartrecruiters" in host:
        return "smartrecruiters"
    if "taleo" in host:
        return "taleo"
    return host or "unknown"

def is_linkedin_url(url):
    host = (urlparse(url or "").hostname or "").lower().lstrip(".")
    return host == "linkedin.com" or host.endswith(".linkedin.com")

def is_linkedin_jobs_search_url(url):
    parsed = urlparse(url or "")
    return is_linkedin_url(url) and (parsed.path or "").startswith("/jobs/search")

def is_external_nonblank_url(url):
    return bool(url) and url != "about:blank" and not is_linkedin_url(url)

def infer_sender_filter(url):
    ats = infer_ats(url)
    if ats == "brassring":
        return "brassring"
    if ats == "greenhouse":
        return "greenhouse"
    if ats == "ashby":
        return "ashby"
    if ats == "lever":
        return "lever"
    if ats == "workday":
        return "workday"
    if ats == "icims":
        return "icims"
    host = (urlparse(url or "").hostname or "").lower()
    parts = host.split(".")
    return parts[-2] if len(parts) >= 2 else host

def get_apply_scopes(page):
    scopes = [page]
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        scopes.append(frame)
    return scopes

def wait_for_dynamic_page(page, timeout=10000):
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass
    page.wait_for_timeout(2000)

def apply_page_readiness_snapshot(page):
    best_text_len = 0
    best_text = ""
    total_controls = 0
    total_form_controls = 0
    url = ""
    try:
        url = page.url
    except Exception:
        pass
    for scope in get_apply_scopes(page):
        try:
            text = (scope.locator("body").inner_text(timeout=800) or "").strip()
            if len(text) > best_text_len:
                best_text_len = len(text)
                best_text = text
        except Exception:
            pass
        try:
            total_form_controls += scope.locator("input, textarea, select").count()
        except Exception:
            pass
        try:
            total_controls += scope.locator('button, input[type="submit"], input[type="button"], a, [role="button"]').count()
        except Exception:
            pass
    text_norm = re.sub(r"\s+", " ", best_text).strip()
    text_low = text_norm.lower()
    return {
        "url": url,
        "text_len": best_text_len,
        "controls": total_controls,
        "form_controls": total_form_controls,
        "has_apply_body": bool(re.search(r"\b(autofill with resume|apply manually|upload resume|create account|sign in|save and continue|review)\b", text_low)),
        "text_preview": text_norm[:220],
    }

def wait_for_apply_page_ready(page, timeout=45000, allow_reload=False):
    start = time.time()
    reloaded = False
    last_snapshot = {}
    while (time.time() - start) * 1000 < timeout:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=2500)
        except Exception:
            pass
        try:
            page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass
        dismiss_popups(page)
        last_snapshot = apply_page_readiness_snapshot(page)
        host = (urlparse(last_snapshot.get("url") or "").hostname or "").lower()
        is_workday = "myworkdayjobs.com" in host
        if last_snapshot.get("form_controls", 0) > 0:
            return {"ready": True, **last_snapshot}
        if is_workday:
            if last_snapshot.get("has_apply_body") and last_snapshot.get("text_len", 0) >= 300:
                return {"ready": True, **last_snapshot}
        elif last_snapshot.get("controls", 0) >= 2 or last_snapshot.get("text_len", 0) >= 120:
            return {"ready": True, **last_snapshot}
        if (
            allow_reload
            and not reloaded
            and (time.time() - start) > 20
            and last_snapshot.get("text_len", 0) < 20
            and last_snapshot.get("controls", 0) == 0
        ):
            print(f"[Access Gate] Page still blank after 20s; refreshing once. snapshot={last_snapshot}")
            try:
                page.reload(wait_until="domcontentloaded", timeout=60000)
                reloaded = True
            except Exception as reload_err:
                print(f"[Access Gate] Refresh failed while waiting for apply page: {reload_err}")
        page.wait_for_timeout(2000)
    print(f"[Access Gate] Page readiness timeout after {timeout}ms. snapshot={last_snapshot}")
    return {"ready": False, **last_snapshot}

def classify_field_risk(label, input_type):
    text = f"{label or ''} {input_type or ''}"
    if input_type == "email" or re.search(r"\b(email|e-mail|phone|first name|last name|full name)\b", text, re.IGNORECASE):
        return "low"
    if HIGH_RISK_FIELD_RE.search(text):
        return "high"
    if MEDIUM_RISK_FIELD_RE.search(text):
        return "medium"
    return "low"

def is_noise_form_field(item):
    input_type = item.get("input_type")
    key = " ".join(str(item.get(name, "")) for name in ["label", "name", "id", "placeholder"]).strip().lower()
    if input_type in {"submit", "button", "image", "reset"}:
        return True
    if input_type == "search" and ("search" in key or re.search(r"(^|\s)s($|\s)", key)):
        return True
    if "title, skills or keywords" in key or "title skills or keywords" in key:
        return True
    if "landingpage-form-input" in key or "closed-jobs-toggle" in key or "show closed contract roles" in key:
        return True
    if not key and input_type in {"input", "text"}:
        return True
    return False

def candidate_profile_value(label, input_type, user_data):
    label_low = (label or "").lower()
    first = user_data.get("first_name", "")
    last = user_data.get("last_name", "")
    def pick(*keys):
        for key in keys:
            value = user_data.get(key)
            if value:
                return value
        return None

    if "first" in label_low and first:
        return first
    if "last" in label_low and last:
        return last
    if "full name" in label_low and (first or last):
        return f"{first} {last}".strip()
    if ("email" in label_low or input_type == "email") and user_data.get("email"):
        return user_data.get("email")
    if (
        "phone" in label_low
        or "mobile" in label_low
        or "cell" in label_low
        or input_type == "tel"
    ) and user_data.get("phone"):
        return user_data.get("phone")
    if "linkedin" in label_low and user_data.get("linkedin_url"):
        return user_data.get("linkedin_url")
    if "github" in label_low and user_data.get("github_url"):
        return user_data.get("github_url")
    if ("portfolio" in label_low or "website" in label_low) and user_data.get("portfolio_url"):
        return user_data.get("portfolio_url")
    if "street" in label_low or "address" in label_low:
        if "other" in label_low or "address 2" in label_low or "address2" in label_low:
            return pick("address2", "street_address_2")
        if "address line 1" in label_low or "address1" in label_low or "street" in label_low:
            return pick("address1", "street_address")
        return pick("address1", "street_address", "address")
    if "city" in label_low:
        return pick("city")
    if "state" in label_low or "province" in label_low or "region" in label_low:
        return pick("state", "province", "region")
    if "zip" in label_low or "postal" in label_low:
        return pick("zip", "zipcode", "postal_code")
    if "country" in label_low:
        return pick("country") or "United States"
    return None

def canonicalize_field(label, input_type=None, name="", field_id="", placeholder=""):
    text = " ".join(str(part or "") for part in [label, name, field_id, placeholder, input_type]).lower()
    text = re.sub(r"[_\-]+", " ", text)

    if input_type == "file" or re.search(r"\b(resume|cv|curriculum vitae|upload)\b", text):
        return "resume_file"
    if "cover letter" in text:
        return "cover_letter"
    if re.search(r"\b(first|given)\s+name\b", text):
        return "first_name"
    if re.search(r"\b(last|family|surname)\s+name\b", text):
        return "last_name"
    if "full name" in text or text.strip() == "name":
        return "full_name"
    if "email" in text or input_type == "email":
        return "email"
    if "phone" in text or "mobile" in text or input_type == "tel":
        return "phone"
    if "linkedin" in text:
        return "linkedin_url"
    if "github" in text:
        return "github_url"
    if "portfolio" in text or "website" in text or "personal site" in text:
        return "portfolio_url"
    if re.search(r"\b(work authorization|authorized to work|legally authorized)\b", text):
        return "work_authorization"
    if "sponsor" in text or "visa" in text:
        return "requires_sponsorship"
    if "salary" in text or "compensation" in text:
        return "salary_expectation"
    if "start date" in text or "available to start" in text:
        return "start_date"
    if "street" in text or "address" in text:
        return "address"
    if "city" in text:
        return "city"
    if "state" in text or "province" in text or "region" in text:
        return "state_region"
    if "zip" in text or "postal" in text:
        return "postal_code"
    if "country" in text:
        return "country"
    if "degree" in text or "education" in text or "school" in text:
        return "education"
    if "veteran" in text:
        return "veteran_status"
    if "disability" in text:
        return "disability_status"
    if "gender" in text:
        return "gender"
    if "race" in text or "ethnicity" in text:
        return "race_ethnicity"
    if "certify" in text or "accurate" in text or "truthful" in text:
        return "legal_certification"
    return "unknown"

def stable_field_id(field):
    raw = "|".join(str(field.get(key, "")) for key in [
        "scope_index", "selector", "name", "id", "label", "input_type"
    ])
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:10]
    return f"field_{digest}"

def locator_candidates_for_field(field):
    candidates = []

    def add(strategy, value, confidence):
        value = str(value or "").strip()
        if not value:
            return
        item = {"strategy": strategy, "value": value, "confidence": confidence}
        if item not in candidates:
            candidates.append(item)

    label = field.get("label") or field.get("raw_label")
    tag = field.get("tag") or "input"
    input_type = field.get("input_type") or ""
    add("label", label, 0.96)
    add("placeholder", field.get("placeholder"), 0.88)
    if field.get("id"):
        add("css", f'{tag}#{field.get("id")}', 0.86)
    if field.get("name"):
        add("css", f'{tag}[name="{quoted_css_attr(field.get("name"))}"]', 0.84)
    if input_type and tag == "input":
        add("css", f'input[type="{quoted_css_attr(input_type)}"]', 0.64)
    add("css", field.get("selector"), 0.72)
    return candidates

def normalize_page_state_field(field, user_data=None):
    user_data = user_data or {}
    normalized = dict(field)
    raw_label = normalized.get("raw_label") or normalized.get("label") or ""
    canonical = canonicalize_field(
        raw_label,
        normalized.get("input_type"),
        normalized.get("name"),
        normalized.get("id"),
        normalized.get("placeholder"),
    )
    risk = normalized.get("risk") or classify_field_risk(raw_label, normalized.get("input_type"))
    answer_preview = candidate_profile_value(raw_label, normalized.get("input_type"), user_data)
    if canonical in {"resume_file"}:
        answer_preview = "resume_path" if answer_preview is None else answer_preview

    normalized.update({
        "field_id": normalized.get("field_id") or stable_field_id(normalized),
        "raw_label": raw_label,
        "canonical_field": canonical,
        "risk": risk,
        "risk_level": risk,
        "locator_candidates": normalized.get("locator_candidates") or locator_candidates_for_field(normalized),
        "answer_source": "profile" if answer_preview and canonical != "resume_file" else ("resume_path" if canonical == "resume_file" else None),
        "answer_preview": answer_preview if risk == "low" else None,
        "auto_fill_allowed": risk == "low",
        "requires_confirmation": risk in {"high", "medium"} and bool(normalized.get("required")),
    })
    return normalized

def extract_form_schema(page, user_data=None):
    user_data = user_data or {}
    fields = []
    body_text_for_fields = page_body_text(page, timeout=1000)
    body_text_low = body_text_for_fields.lower()
    js = """
    (elements) => {
      const esc = (value) => {
        if (window.CSS && CSS.escape) return CSS.escape(String(value));
        return String(value).replace(/["\\\\]/g, "\\\\$&");
      };
      const isVisible = (el) => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return !!(rect.width || rect.height || el.getClientRects().length) &&
          style.visibility !== "hidden" && style.display !== "none";
      };
      const cleanText = (text) => String(text || "").replace(/\\s+/g, " ").trim();
      const groupContainerFor = (el) => {
        return el.closest("fieldset, [role='radiogroup'], .fieldcontain, [class*='question-'][class*='container'], .question, .form-group, li");
      };
      const groupTextFor = (el, labels) => {
        const container = groupContainerFor(el);
        if (!container) return "";
        const text = cleanText(container.innerText);
        const optionText = cleanText(labels.join(" "));
        if (!text || text === optionText || text.length > 900) return "";
        return text;
      };
      const labelFor = (el) => {
        const tag = el.tagName.toLowerCase();
        const type = tag === "select" ? "select" : (el.getAttribute("type") || tag).toLowerCase();
        const labels = el.labels ? Array.from(el.labels).map(label => label.innerText.trim()).filter(Boolean) : [];
        if (type === "radio") {
          const groupText = groupTextFor(el, labels);
          if (groupText) return labels.length ? `${groupText} ${labels.join(" ")}` : groupText;
        }
        if (labels.length) return labels.join(" ");
        const ariaLabel = el.getAttribute("aria-label");
        if (ariaLabel) return ariaLabel;
        const ariaLabelledBy = el.getAttribute("aria-labelledby");
        if (ariaLabelledBy) {
          const text = ariaLabelledBy.split(/\\s+/)
            .map(id => document.getElementById(id))
            .filter(Boolean)
            .map(node => node.innerText.trim())
            .filter(Boolean)
            .join(" ");
          if (text) return text;
        }
        const placeholder = el.getAttribute("placeholder");
        if (placeholder) return placeholder;
        const id = el.getAttribute("id");
        if (id) {
          const label = document.querySelector(`label[for="${esc(id)}"]`);
          if (label && label.innerText.trim()) return label.innerText.trim();
        }
        const parentLabel = el.closest("label");
        if (parentLabel && parentLabel.innerText.trim()) return parentLabel.innerText.trim();
        const container = el.closest("[role='group'], .form-group, .field, .question, li, div");
        if (container) {
          const containerLabel = container.querySelector("label, legend, .label, .field-label, [id*='label' i]");
          if (containerLabel && containerLabel.innerText.trim()) return containerLabel.innerText.trim();
        }
        return el.getAttribute("name") || el.getAttribute("id") || "";
      };
      const selectorFor = (el, index) => {
        const tag = el.tagName.toLowerCase();
        const id = el.getAttribute("id");
        if (id) return `${tag}#${esc(id)}`;
        const name = el.getAttribute("name");
        if (name) return `${tag}[name="${esc(name)}"]`;
        return `${tag}:nth-of-type(${index + 1})`;
      };
      return elements.map((el, index) => {
        const tag = el.tagName.toLowerCase();
        const type = tag === "select" ? "select" : (el.getAttribute("type") || tag).toLowerCase();
        const groupContainer = groupContainerFor(el);
        const groupText = groupContainer ? cleanText(groupContainer.innerText) : "";
        const options = tag === "select"
          ? Array.from(el.options || []).map(option => option.innerText.trim()).filter(Boolean).slice(0, 40)
          : [];
        return {
          label: labelFor(el),
          tag,
          input_type: type,
          name: el.getAttribute("name") || "",
          id: el.getAttribute("id") || "",
          placeholder: el.getAttribute("placeholder") || "",
          required: !!el.required || el.getAttribute("aria-required") === "true" || ((type === "radio" || type === "checkbox") && /(^|\\s|\\*)Required\\b/i.test(groupText)),
          disabled: !!el.disabled,
          read_only: !!el.readOnly,
          value_present: !!el.value,
          checked: !!el.checked,
          options,
          selector: selectorFor(el, index),
          visible: isVisible(el)
        };
      }).filter(item => item.visible && !item.disabled);
    }
    """
    for scope_index, scope in enumerate(get_apply_scopes(page)):
        try:
            locator = scope.locator("input, textarea, select")
            count = locator.count()
            if count == 0:
                continue
            extracted = locator.evaluate_all(js)
            scope_url = getattr(scope, "url", page.url)
            for item in extracted:
                label = item.get("label") or item.get("placeholder") or item.get("name") or item.get("id")
                input_type = item.get("input_type")
                if input_type == "file" and "upload resume" in body_text_low:
                    label = label or "Upload Resume"
                    item["required"] = True
                if is_noise_form_field(item):
                    continue
                risk = classify_field_risk(label, input_type)
                answer_preview = candidate_profile_value(label, input_type, user_data)
                item.update({
                    "scope_index": scope_index,
                    "scope_url": scope_url,
                    "raw_label": label,
                    "label": label,
                    "risk": risk,
                    "autofill_allowed": risk == "low",
                    "answer_preview": answer_preview if risk == "low" else None,
                    "needs_user_input": risk != "low" and bool(item.get("required"))
                })
                fields.append(normalize_page_state_field(item, user_data))
        except Exception as err:
            print(f"[Access Gate] Field extraction skipped for one scope: {err}")
    return fields

def page_body_text(page, timeout=3000):
    try:
        return compact_text(page.locator("body").inner_text(timeout=timeout))
    except Exception:
        return ""

def infer_apply_stage(page, fields):
    text = page_body_text(page)[:16000].lower()
    url = page.url.lower()
    try:
        title = (page.title(timeout=1000) or "").lower()
    except Exception:
        title = ""
    password_count = sum(1 for f in fields if f.get("input_type") == "password")
    code_count = sum(
        1 for f in fields
        if re.search(r"(verification|one[-\s]?time|otp|pin|security\s+code)", f"{f.get('label','')} {f.get('name','')} {f.get('id','')}", re.IGNORECASE)
    )
    visible_field_count = len([f for f in fields if f.get("input_type") not in {"hidden", "submit", "button"}])

    if "captcha" in text or "recaptcha" in text or "hcaptcha" in text:
        return "blocked_captcha"
    if ("we use cookies" in text or "tracking technologies" in text or "cookie" in text) and "accept" in text:
        return "privacy_policy"
    if ("privacy policy" in text or "terms of use" in text or "data protection" in text) and "agree" in text:
        return "privacy_policy"
    if (
        "custom job error" in title
        or "page was removed" in text
        or "job is no longer available" in text
        or "position is no longer available" in text
        or ("explore open roles" in text and "sorry" in text)
    ):
        return "job_closed"
    if (
        code_count
        or "verification code" in text
        or "one-time" in text
        or "check your email" in text
        or ("send email" in text and ("receive a link" in text or "email address provided" in text))
    ):
        return "email_verification"
    if "upload resume" in text and "next" in text:
        return "application_form"
    if "choose your sign in option" in text or any(
        re.search(r"loginfield|login_field|signin|sign_in", f"{f.get('name','')} {f.get('id','')}", re.IGNORECASE)
        for f in fields
    ):
        return "sign_in"
    if password_count:
        if password_count >= 2 or any(token in text for token in ["create account", "create profile", "register", "sign up", "new user"]):
            return "create_account"
        return "sign_in"
    if visible_field_count >= 2 and not any(token in text for token in ["search jobs", "keyword", "job alerts"]):
        return "application_form"
    if any(token in text for token in ["create account", "create profile", "new user", "register", "sign up"]):
        return "sign_in"
    if any(token in text for token in ["apply", "start application", "begin application", "continue applying"]):
        return "job_detail"
    if any(token in url for token in ["jobdetails", "jobdetail", "jobdetails="]):
        return "job_detail"
    return "unknown"

def extract_action_buttons(page):
    buttons = []
    js = """
    (elements) => {
      const esc = (value) => {
        if (window.CSS && CSS.escape) return CSS.escape(String(value));
        return String(value).replace(/["\\\\]/g, "\\\\$&");
      };
      const cleanText = (text) => String(text || "").replace(/\\s+/g, " ").trim();
      const isVisible = (el) => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return !!(rect.width || rect.height || el.getClientRects().length) &&
          style.visibility !== "hidden" && style.display !== "none";
      };
      const selectorFor = (el, index) => {
        const tag = el.tagName.toLowerCase();
        const id = el.getAttribute("id");
        if (id) return `${tag}#${esc(id)}`;
        const name = el.getAttribute("name");
        if (name) return `${tag}[name="${esc(name)}"]`;
        return `${tag}:nth-of-type(${index + 1})`;
      };
      const labelFor = (el) => {
        return cleanText(
          el.getAttribute("aria-label") ||
          el.innerText ||
          el.value ||
          el.title ||
          el.getAttribute("name") ||
          el.getAttribute("id") ||
          ""
        );
      };
      return elements.map((el, index) => {
        const tag = el.tagName.toLowerCase();
        const role = el.getAttribute("role") || (tag === "a" ? "link" : "button");
        const text = labelFor(el);
        return {
          text,
          role,
          tag,
          type: (el.getAttribute("type") || "").toLowerCase(),
          enabled: !el.disabled && el.getAttribute("aria-disabled") !== "true",
          visible: isVisible(el),
          selector: selectorFor(el, index)
        };
      }).filter(item => item.visible && item.text);
    }
    """
    for scope_index, scope in enumerate(get_apply_scopes(page)):
        try:
            locator = scope.locator('button, a, [role="button"], input[type="submit"], input[type="button"]')
            extracted = locator.evaluate_all(js)
            for item in extracted[:80]:
                text = item.get("text") or ""
                item.update({
                    "button_id": f"button_{hashlib.sha1((str(scope_index) + '|' + item.get('selector', '') + '|' + text).encode('utf-8', errors='ignore')).hexdigest()[:10]}",
                    "scope_index": scope_index,
                    "scope_url": getattr(scope, "url", page.url),
                    "is_final_submit": bool(FINAL_SUBMIT_RE.search(text)),
                    "risk_level": "irreversible" if FINAL_SUBMIT_RE.search(text) else "low",
                    "locator_candidates": [
                        {"strategy": "role", "value": text, "confidence": 0.92},
                        {"strategy": "text", "value": text, "confidence": 0.82},
                        {"strategy": "css", "value": item.get("selector"), "confidence": 0.72},
                    ]
                })
                buttons.append(item)
        except Exception as err:
            print(f"[Access Gate] Button extraction skipped for one scope: {err}")
    return buttons

def extract_validation_errors(page):
    errors = []
    selectors = [
        '[role="alert"]',
        '[aria-live="assertive"]',
        '[aria-live="polite"]',
        '.error',
        '.errors',
        '.field-error',
        '.form-error',
        '.validation-error',
        '[class*="error" i]',
        '[data-automation-id*="error" i]',
    ]
    for scope_index, scope in enumerate(get_apply_scopes(page)):
        seen = set()
        for selector in selectors:
            try:
                locators = scope.locator(selector)
                for index in range(min(locators.count(), 20)):
                    locator = locators.nth(index)
                    if not locator.is_visible(timeout=300):
                        continue
                    text = compact_text(locator.inner_text(timeout=500))
                    if not text or len(text) > 500 or text in seen:
                        continue
                    seen.add(text)
                    errors.append({
                        "scope_index": scope_index,
                        "text": text,
                        "selector": selector,
                    })
            except Exception:
                continue
        try:
            invalids = scope.locator('[aria-invalid="true"]')
            for index in range(min(invalids.count(), 20)):
                locator = invalids.nth(index)
                label = locator_label(locator)
                if label:
                    text = f"Invalid field: {label}"
                    if text not in seen:
                        seen.add(text)
                        errors.append({
                            "scope_index": scope_index,
                            "text": text,
                            "selector": '[aria-invalid="true"]',
                        })
        except Exception:
            pass
    return errors[:40]

def accessibility_snapshot(page, limit=12000):
    try:
        body = page.locator("body")
        aria_snapshot = getattr(body, "aria_snapshot", None)
        if not aria_snapshot:
            return None
        snapshot = aria_snapshot(timeout=2500)
        if isinstance(snapshot, str):
            return snapshot[:limit]
        return str(snapshot)[:limit]
    except Exception as err:
        print(f"[Access Gate] Accessibility snapshot skipped: {err}")
        return None

def build_page_state(page, fields=None, user_data=None, stage=None):
    fields = fields if fields is not None else extract_form_schema(page, user_data)
    stage = stage or infer_apply_stage(page, fields)
    buttons = extract_action_buttons(page)
    validation_errors = extract_validation_errors(page)
    title = ""
    try:
        title = page.title()
    except Exception:
        pass
    ax_snapshot = accessibility_snapshot(page)
    return {
        "url": page.url,
        "page_title": title,
        "detected_ats": infer_ats(page.url),
        "step": stage,
        "forms": [
            {
                "form_id": "current_page",
                "fields": fields,
                "buttons": buttons,
                "validation_errors": validation_errors,
            }
        ],
        "summary": {
            "fields_count": len(fields),
            "required_fields_count": len([f for f in fields if f.get("required")]),
            "buttons_count": len(buttons),
            "validation_errors_count": len(validation_errors),
            "high_risk_fields_count": len([f for f in fields if f.get("risk") == "high"]),
            "final_submit_visible": any(button.get("is_final_submit") for button in buttons),
        },
        "observation_layers": {
            "used_dom": True,
            "used_accessibility_tree": bool(ax_snapshot),
            "used_screenshot": False,
        },
        "accessibility_snapshot": ax_snapshot,
    }

def build_preflight(page, fields, user_data, req, stage=None):
    required_fields = [
        field for field in fields
        if field.get("required") and not field.get("value_present") and field.get("input_type") not in {"hidden", "submit", "button"}
    ]
    fill_plan = []
    needs_user = []
    blocking_issues = []
    skipped = []
    resume_available = bool(req.resume_path and os.path.exists(req.resume_path))
    allow_placeholders = allow_placeholder_autofill_for_page(page, req)

    for field in fields:
        input_type = field.get("input_type")
        if field.get("disabled") or field.get("read_only") or input_type in {"hidden", "submit", "button"}:
            continue
        key = field_key(field)
        if field.get("value_present") or (input_type in {"checkbox", "radio"} and field.get("checked")):
            skipped.append({"field": key, "reason": "already_has_value", "risk": field.get("risk")})
            continue
        if field.get("risk") == "high":
            issue = {
                "type": "high_risk_confirmation_required",
                "field": key,
                "field_id": field.get("field_id"),
                "canonical_field": field.get("canonical_field"),
                "risk": field.get("risk"),
                "reason": "High-risk legal or demographic field must use user confirmation/preset answer."
            }
            if field.get("required") or input_type in {"radio", "checkbox", "select"}:
                blocking_issues.append(issue)
            needs_user.append(issue)
            continue

        if input_type == "file" or field.get("canonical_field") == "resume_file":
            if resume_available and req.allow_resume_upload:
                fill_plan.append({
                    "action_type": "upload_file",
                    "field_id": field.get("field_id"),
                    "canonical_field": "resume_file",
                    "file_uri": req.resume_path,
                    "confidence": 1.0,
                    "risk_level": "medium",
                    "requires_confirmation": False,
                })
            elif field.get("required"):
                issue = {
                    "type": "missing_resume_file",
                    "field": key,
                    "field_id": field.get("field_id"),
                    "risk": "medium",
                    "reason": "Required resume upload has no local resume_path."
                }
                blocking_issues.append(issue)
                needs_user.append(issue)
            continue

        if is_safe_discovery_checkbox(field):
            fill_plan.append({
                "action_type": "check_field",
                "field_id": field.get("field_id"),
                "canonical_field": field.get("canonical_field"),
                "value": True,
                "answer_source": "safe_navigation_checkbox",
                "confidence": 0.82,
                "risk_level": field.get("risk"),
                "locator_candidates": field.get("locator_candidates", []),
            })
            continue

        answer = candidate_profile_value(field.get("label"), input_type, user_data)
        source = "profile"
        if not answer and allow_placeholders:
            answer = placeholder_discovery_value(field)
            source = "discovery_placeholder" if answer else None

        if answer and req.allow_low_risk_autofill:
            fill_plan.append({
                "action_type": "fill_field",
                "field_id": field.get("field_id"),
                "canonical_field": field.get("canonical_field"),
                "value": answer,
                "answer_source": source,
                "confidence": 0.95 if source == "profile" else 0.55,
                "risk_level": field.get("risk"),
                "locator_candidates": field.get("locator_candidates", []),
                "requires_confirmation": field.get("risk") != "low",
            })
            continue

        if field_requires_user(field, user_data, req, allow_placeholders=allow_placeholders):
            issue = {
                "type": "missing_answer",
                "field": key,
                "field_id": field.get("field_id"),
                "canonical_field": field.get("canonical_field"),
                "risk": field.get("risk"),
                "reason": "No candidate profile value or safe placeholder is available."
            }
            blocking_issues.append(issue)
            needs_user.append(issue)

    final_submit_visible = page_has_final_submit(page)
    if stage == "blocked_captcha":
        blocking_issues.append({
            "type": "captcha_or_bot_challenge",
            "reason": "Human verification is present; do not bypass it."
        })
    if final_submit_visible:
        blocking_issues.append({
            "type": "final_submit_guard",
            "reason": "Final submission control is visible and requires human confirmation."
        })

    non_final_blocking_issues = [
        item for item in blocking_issues
        if item.get("type") != "final_submit_guard"
    ]

    if stage == "blocked_captcha":
        page_status = "blocked"
    elif non_final_blocking_issues:
        page_status = "needs_user"
    elif final_submit_visible:
        page_status = "review_required"
    elif fields:
        page_status = "fillable"
    else:
        page_status = "no_form_detected"

    return {
        "page_status": page_status,
        "stage": stage,
        "required_fields_count": len(required_fields),
        "auto_fillable_count": len(fill_plan),
        "needs_user_count": len(needs_user),
        "blocking_issues": blocking_issues,
        "fill_plan": fill_plan,
        "skipped": skipped[:40],
        "final_submit_visible": final_submit_visible,
        "resume_available": resume_available,
    }

def is_post_auth_job_listing(page, original_url):
    current = urlparse(page.url or "")
    original = urlparse(original_url or "")
    if not original_url or current.hostname != original.hostname:
        return False
    path = (current.path or "").rstrip("/").lower()
    text = page_body_text(page, timeout=1500).lower()
    if path.endswith("/jobs") and any(token in text for token in [
        "title, skills or keywords",
        "show closed contract roles",
        "contract roles"
    ]):
        return True
    return False

def locator_label(locator):
    try:
        return compact_text(locator.evaluate("""
            el => {
              const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
              const labels = el.labels ? Array.from(el.labels).map(label => clean(label.innerText)).filter(Boolean) : [];
              if (labels.length) return labels.join(" ");
              const ariaLabel = el.getAttribute("aria-label");
              if (ariaLabel) return ariaLabel;
              const ariaLabelledBy = el.getAttribute("aria-labelledby");
              if (ariaLabelledBy) {
                const text = ariaLabelledBy.split(/\\s+/)
                  .map(id => document.getElementById(id))
                  .filter(Boolean)
                  .map(node => clean(node.innerText))
                  .filter(Boolean)
                  .join(" ");
                if (text) return text;
              }
              const id = el.getAttribute("id");
              if (id && window.CSS && CSS.escape) {
                const label = document.querySelector(`label[for="${CSS.escape(id)}"]`);
                if (label && clean(label.innerText)) return clean(label.innerText);
              }
              const parentLabel = el.closest("label");
              if (parentLabel && clean(parentLabel.innerText)) return clean(parentLabel.innerText);
              return clean(el.innerText || el.value || el.title || "");
            }
        """))
    except Exception:
        try:
            return compact_text(locator.inner_text(timeout=500))
        except Exception:
            return ""

def click_matching_control(page, patterns, skip_final_submit=True, avoid_patterns=None):
    compiled = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    avoid_compiled = [re.compile(pattern, re.IGNORECASE) for pattern in (avoid_patterns or [])]
    for scope in get_apply_scopes(page):
        for role in ["button", "link"]:
            for regex in compiled:
                try:
                    locator = scope.get_by_role(role, name=regex).first
                    if locator.count() == 0 or not locator.is_visible(timeout=1000):
                        continue
                    label = locator_label(locator)
                    if skip_final_submit and FINAL_SUBMIT_RE.search(label):
                        continue
                    if any(avoid.search(label) for avoid in avoid_compiled):
                        continue
                    locator.click(timeout=4000)
                    page.wait_for_timeout(2500)
                    return True, label or regex.pattern
                except Exception:
                    continue
        for selector in ['input[type="submit"]', 'input[type="button"]']:
            try:
                controls = scope.locator(selector)
                for index in range(min(controls.count(), 10)):
                    locator = controls.nth(index)
                    if not locator.is_visible(timeout=500):
                        continue
                    label = locator_label(locator)
                    if not any(regex.search(label) for regex in compiled):
                        continue
                    if skip_final_submit and FINAL_SUBMIT_RE.search(label):
                        continue
                    if any(avoid.search(label) for avoid in avoid_compiled):
                        continue
                    locator.click(timeout=4000)
                    page.wait_for_timeout(2500)
                    return True, label
            except Exception:
                continue
        for selector in ['button', 'a', '[role="button"]', 'div[tabindex]', 'div[class*="button"]', 'div[class*="Button"]', 'span[role="button"]']:
            try:
                controls = scope.locator(selector)
                for index in range(min(controls.count(), 40)):
                    locator = controls.nth(index)
                    if not locator.is_visible(timeout=500):
                        continue
                    label = locator_label(locator)
                    if not label or len(label) > 120:
                        continue
                    if not any(regex.search(label) for regex in compiled):
                        continue
                    if skip_final_submit and FINAL_SUBMIT_RE.search(label):
                        continue
                    if any(avoid.search(label) for avoid in avoid_compiled):
                        continue
                    locator.click(timeout=4000)
                    page.wait_for_timeout(2500)
                    return True, label
            except Exception:
                continue
    return False, ""

def click_workday_application_choice(page, prefer_resume=False):
    host = (urlparse(page.url or "").hostname or "").lower()
    if "myworkdayjobs.com" not in host:
        return False, ""
    text = page_body_text(page, timeout=1500).lower()
    if "start your application" not in text:
        return False, ""
    label_order = ["Autofill with Resume", "Apply Manually"] if prefer_resume else ["Apply Manually", "Autofill with Resume"]
    for scope in get_apply_scopes(page):
        for role in ["button", "link"]:
            for label in label_order:
                try:
                    locator = scope.get_by_role(role, name=re.compile(rf"^{re.escape(label)}$", re.IGNORECASE)).first
                    if locator.count() == 0 or not locator.is_visible(timeout=1000):
                        continue
                    locator.click(timeout=5000)
                    page.wait_for_timeout(3000)
                    return True, label
                except Exception:
                    continue
        try:
            controls = scope.locator('button, a, [role="button"]')
            for index in range(min(controls.count(), 30)):
                locator = controls.nth(index)
                if not locator.is_visible(timeout=500):
                    continue
                label = locator_label(locator)
                for expected in label_order:
                    if re.fullmatch(rf"\s*{re.escape(expected)}\s*", label or "", re.IGNORECASE):
                        locator.click(timeout=5000)
                        page.wait_for_timeout(3000)
                        return True, label
        except Exception:
            continue
    return False, ""

def parse_model_json(text):
    action_text = (text or "").strip()
    if action_text.startswith("```json"):
        action_text = action_text.replace("```json", "", 1).replace("```", "").strip()
    elif action_text.startswith("```"):
        action_text = action_text.replace("```", "").strip()
    return json.loads(action_text)

def element_label_at_point(page, x, y):
    try:
        return compact_text(page.evaluate("""
            ([x, y]) => {
              const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
              let el = document.elementFromPoint(x, y);
              if (!el) return "";
              const parts = [];
              for (let i = 0; el && i < 4; i++, el = el.parentElement) {
                const label = clean(el.getAttribute("aria-label") || el.innerText || el.value || el.title || "");
                if (label) parts.push(label);
              }
              return parts.join(" ");
            }
        """, [x, y]))
    except Exception:
        return ""

def upload_resume_file(page, resume_path, x=None, y=None):
    if not resume_path or not os.path.exists(resume_path):
        return False, "resume_path_missing"
    for scope in get_apply_scopes(page):
        try:
            file_inputs = scope.locator('input[type="file"]')
            if file_inputs.count() > 0:
                file_inputs.first.set_input_files(resume_path, timeout=5000)
                page.wait_for_timeout(3000)
                return True, "input[type=file]"
        except Exception:
            pass
    if x is not None and y is not None:
        try:
            with page.expect_file_chooser(timeout=5000) as fc_info:
                page.mouse.click(x, y)
            fc_info.value.set_files(resume_path)
            page.wait_for_timeout(3000)
            return True, "file_chooser"
        except Exception as err:
            return False, f"file_chooser_failed:{err}"
    return False, "no_file_input"

def upload_resume_via_visible_control(page, resume_path):
    if not resume_path or not os.path.exists(resume_path):
        return False, "resume_path_missing"
    patterns = [
        re.compile(r"\b(upload|select|choose|attach)\b.*\b(resume|cv)\b", re.IGNORECASE),
        re.compile(r"\b(resume|cv)\b.*\b(upload|select|choose|attach)\b", re.IGNORECASE),
    ]
    for scope in get_apply_scopes(page):
        try:
            controls = scope.locator('button, a, [role="button"], input[type="button"], input[type="submit"]')
            for index in range(min(controls.count(), 80)):
                locator = controls.nth(index)
                try:
                    if not locator.is_visible(timeout=300):
                        continue
                    label = locator_label(locator)
                    if not label or not any(pattern.search(label) for pattern in patterns):
                        continue
                    if FINAL_SUBMIT_RE.search(label):
                        continue
                    with page.expect_file_chooser(timeout=5000) as fc_info:
                        locator.click(timeout=3000, force=True)
                    fc_info.value.set_files(resume_path)
                    page.wait_for_timeout(3000)
                    return True, f"file_chooser:{label}"
                except Exception as err:
                    last_error = str(err)
                    continue
        except Exception:
            continue
    return False, locals().get("last_error", "no_resume_upload_control")

def handle_resume_upload_prompt(page, req):
    if not req.allow_resume_upload:
        return {"attempted": False, "reason": "resume_upload_disabled"}
    if not req.resume_path or not os.path.exists(req.resume_path):
        return {"attempted": False, "reason": "resume_path_missing"}
    text = page_body_text(page, timeout=1500).lower()
    has_resume_prompt = any(token in text for token in [
        "apply with resume",
        "upload resume",
        "select resume to upload",
        "fill out application with my resume",
        "autofill with resume",
    ])
    file_inputs_seen = 0
    try:
        for scope in get_apply_scopes(page):
            file_inputs_seen += scope.locator('input[type="file"]').count()
    except Exception:
        file_inputs_seen = 0
    if not has_resume_prompt and file_inputs_seen == 0:
        return {"attempted": False, "reason": "no_resume_prompt"}

    ok, method = upload_resume_via_visible_control(page, req.resume_path)
    if ok:
        return {"attempted": True, "uploaded": True, "method": method}

    ok, method = upload_resume_file(page, req.resume_path)
    return {
        "attempted": True,
        "uploaded": ok,
        "method": method,
    }

def visual_access_step(page, req, user_data, reason="unknown"):
    if not client:
        return {"acted": False, "reason": "vision_client_unavailable"}

    screenshot_dir = os.path.join(os.path.dirname(__file__), "..", "screenshots")
    os.makedirs(screenshot_dir, exist_ok=True)
    screenshot_bytes = page.screenshot(type="png", full_page=False)
    img_w, img_h = get_png_dimensions(screenshot_bytes)
    screenshot_path = os.path.join(screenshot_dir, f"apply_visual_{int(time.time())}.png")
    try:
        with open(screenshot_path, "wb") as f:
            f.write(screenshot_bytes)
    except Exception:
        screenshot_path = ""

    page_text = page_body_text(page, timeout=1500)[:5000]
    has_resume = bool(req.resume_path and os.path.exists(req.resume_path))
    image_part = types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png")
    prompt = f"""
You are controlling a job application PRECHECK browser flow. The page has loaded but deterministic rules need help.

Current URL: {page.url}
Reason for visual fallback: {reason}
Screenshot resolution: {img_w}x{img_h} physical pixels.
Resume PDF available for upload: {has_resume}
Page text excerpt:
{page_text}

Choose exactly one safe next action. Allowed actions:
- "click": click a visible button/link such as Apply, Apply Manually, Autofill with Resume, Sign In, Create Account, Continue, Next, Review.
- "upload_resume": upload the available PDF resume when the current page asks for a resume or file upload.
- "scroll": scroll down if the required action is below the fold.
- "wait": wait when the page is still loading/spinning.
- "stop": stop when blocked, when human input is required, or when the page is at final submission.

Hard safety rules:
- NEVER choose or click final submission controls: Submit Application, Send Application, Complete Application, Finish Application, Final Submit.
- If the page is ready for final submission, return action "stop" and stop_reason "final_submit_guard".
- Do not invent answers to sensitive questions. This visual fallback is for navigation/upload only.
- Prefer "Autofill with Resume" when a resume PDF is available. Prefer "Apply Manually" only when no resume upload path is visible.

Return ONLY valid JSON:
{{
  "action": "click" | "upload_resume" | "scroll" | "wait" | "stop",
  "target": "short visible label or reason",
  "x": integer_or_null,
  "y": integer_or_null,
  "scroll_direction": "down" | "up",
  "stop_reason": "final_submit_guard|human_required|captcha|blocked|no_safe_action|loading",
  "reason": "one concise sentence"
}}
"""
    try:
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=[image_part, prompt],
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0),
        )
        action_json = parse_model_json(response.text)
    except Exception as err:
        return {"acted": False, "reason": f"vision_query_failed:{err}", "screenshot_path": screenshot_path}

    action = str(action_json.get("action") or "stop").lower()
    target = str(action_json.get("target") or "")
    result = {
        "acted": False,
        "action": action,
        "target": target,
        "reason": action_json.get("reason"),
        "stop_reason": action_json.get("stop_reason"),
        "screenshot_path": screenshot_path,
    }

    if FINAL_SUBMIT_RE.search(target):
        result["stop_reason"] = "final_submit_guard"
        result["reason"] = "visual target matched final submit guard"
        return result
    if action == "stop":
        return result
    if action == "wait":
        page.wait_for_timeout(5000)
        result["acted"] = True
        return result
    if action == "scroll":
        direction = str(action_json.get("scroll_direction") or "down").lower()
        page.evaluate(f"window.scrollBy(0, {600 if direction != 'up' else -600})")
        page.wait_for_timeout(1500)
        result["acted"] = True
        return result

    x_raw = action_json.get("x")
    y_raw = action_json.get("y")
    x, y = scale_coordinates(x_raw, y_raw, img_w, img_h, page)
    if action == "upload_resume":
        ok, method = upload_resume_file(page, req.resume_path, x, y)
        result["acted"] = ok
        result["upload_method"] = method
        return result
    if action == "click":
        if target:
            clicked, label = click_matching_control(
                page,
                [rf"^{re.escape(target)}$", re.escape(target)],
                skip_final_submit=True,
            )
            if clicked:
                result["acted"] = True
                result["element_label"] = label
                return result
        if x is None or y is None:
            result["reason"] = "visual_click_missing_coordinates"
            return result
        label = element_label_at_point(page, x, y)
        result["element_label"] = label
        if FINAL_SUBMIT_RE.search(label):
            result["stop_reason"] = "final_submit_guard"
            result["reason"] = "clicked element matched final submit guard"
            return result
        page.mouse.click(x, y)
        page.wait_for_timeout(3000)
        result["acted"] = True
        return result

    result["reason"] = "no_executable_visual_action"
    return result

def run_visual_fallback(page, req, user_data, history, reason):
    if not req.allow_visual_fallback:
        return False, None
    visual_result = visual_access_step(page, req, user_data, reason=reason)
    if history:
        history[-1]["visual_fallback"] = visual_result
    if visual_result.get("acted"):
        return True, None
    stop_reason = visual_result.get("stop_reason")
    if stop_reason in {"final_submit_guard", "human_required", "captcha", "blocked", "no_safe_action"}:
        return False, stop_reason
    return False, None

def fill_first_available_scopes(page, selectors, value):
    if not value:
        return False
    for scope in get_apply_scopes(page):
        for selector in selectors:
            try:
                locator = scope.locator(selector).first
                if locator.count() == 0 or not locator.is_visible(timeout=1000):
                    continue
                locator.fill(str(value), timeout=3000)
                return True
            except Exception:
                continue
    return False

def fill_visible_passwords(page, password):
    if not password:
        return 0
    filled = 0
    for scope in get_apply_scopes(page):
        try:
            locators = scope.locator('input[type="password"]')
            for index in range(min(locators.count(), 6)):
                locator = locators.nth(index)
                if locator.is_visible(timeout=1000):
                    locator.fill(password, timeout=3000)
                    filled += 1
        except Exception:
            continue
    return filled

def select_security_questions(page):
    selected = 0
    for scope in get_apply_scopes(page):
        selects = scope.locator("select")
        try:
            select_count = min(selects.count(), 10)
        except Exception:
            select_count = 0
        for index in range(select_count):
            try:
                select = selects.nth(index)
                descriptor = ""
                try:
                    descriptor = " ".join([
                        locator_label(select),
                        select.get_attribute("name") or "",
                        select.get_attribute("id") or ""
                    ])
                except Exception:
                    pass
                if not re.search(r"(security|question)", descriptor, re.IGNORECASE):
                    continue

                options = select.evaluate("""
                    el => Array.from(el.options || [])
                      .map(option => ({ value: option.value, text: option.innerText.trim(), disabled: option.disabled }))
                      .filter(option => !option.disabled && option.value && !/select/i.test(option.text))
                """)
                if not options:
                    continue
                choice = options[min(selected, len(options) - 1)]
                try:
                    select.select_option(value=choice["value"], timeout=1500, force=True)
                except Exception:
                    select.evaluate("""
                        (el, value) => {
                          el.value = value;
                          el.dispatchEvent(new Event("input", { bubbles: true }));
                          el.dispatchEvent(new Event("change", { bubbles: true }));
                          if (window.jQuery) window.jQuery(el).trigger("change");
                        }
                    """, choice["value"])
                selected += 1
            except Exception as err:
                print(f"[Access Gate] One security select skipped: {err}")
    return selected

def fill_security_question_answers(page, answers):
    filled = 0
    for scope in get_apply_scopes(page):
        for selector in [
            'input[name*="securityQuestion" i][name*="Answer" i]',
            'input[id*="securityQuestion" i][id*="Answer" i]',
            'input[placeholder*="Answer" i]'
        ]:
            try:
                locators = scope.locator(selector)
                for index in range(min(locators.count(), len(answers))):
                    locator = locators.nth(index)
                    if locator.is_visible(timeout=500):
                        locator.fill(answers[index], timeout=3000)
                        filled += 1
                if filled:
                    return filled
            except Exception:
                continue
    return filled

def fill_security_questions(page, req, user_data):
    if not req.allow_security_question_autofill:
        return {"selected_questions": 0, "filled_answers": 0, "enabled": False}
    answers = get_security_answers(req, user_data)
    return {
        "selected_questions": select_security_questions(page),
        "filled_answers": fill_security_question_answers(page, answers),
        "enabled": True
    }

def fill_auth_identity(page, user_data, password):
    filled = {
        "email": fill_first_available_scopes(page, [
            'input[name*="login" i]',
            'input[id*="login" i]',
            'input[name*="user" i]',
            'input[id*="user" i]',
            'input[type="email"]',
            'input[name*="email" i]',
            'input[id*="email" i]',
            'input[autocomplete="email"]'
        ], user_data.get("email")),
        "first_name": fill_first_available_scopes(page, [
            'input[name*="first" i]',
            'input[id*="first" i]',
            'input[autocomplete="given-name"]'
        ], user_data.get("first_name")),
        "last_name": fill_first_available_scopes(page, [
            'input[name*="last" i]',
            'input[id*="last" i]',
            'input[autocomplete="family-name"]'
        ], user_data.get("last_name")),
        "phone": fill_first_available_scopes(page, [
            'input[type="tel"]',
            'input[name*="phone" i]',
            'input[id*="phone" i]',
            'input[autocomplete="tel"]'
        ], user_data.get("phone")),
        "password_fields": fill_visible_passwords(page, password)
    }
    return filled

def terms_checkbox_count(page):
    count = 0
    for scope in get_apply_scopes(page):
        try:
            boxes = scope.locator('input[type="checkbox"]')
            for index in range(min(boxes.count(), 20)):
                box = boxes.nth(index)
                if not box.is_visible(timeout=500):
                    continue
                descriptor = " ".join([
                    locator_label(box),
                    box.get_attribute("name") or "",
                    box.get_attribute("id") or ""
                ])
                if re.search(r"(agree|terms|privacy|consent|acknowledge)", descriptor, re.IGNORECASE):
                    count += 1
        except Exception:
            continue
    return count

def click_terms_checkboxes(page):
    clicked = 0
    for scope in get_apply_scopes(page):
        try:
            boxes = scope.locator('input[type="checkbox"]')
            for index in range(min(boxes.count(), 20)):
                box = boxes.nth(index)
                if not box.is_visible(timeout=500):
                    continue
                descriptor = " ".join([
                    locator_label(box),
                    box.get_attribute("name") or "",
                    box.get_attribute("id") or ""
                ])
                if not re.search(r"(agree|terms|privacy|consent|acknowledge)", descriptor, re.IGNORECASE):
                    continue
                if box.is_checked(timeout=500):
                    continue
                box.check(timeout=2000, force=True)
                clicked += 1
        except Exception:
            continue
    return clicked

def fill_code_field(page, code):
    selectors = [
        'input[autocomplete="one-time-code"]',
        'input[name*="code" i]',
        'input[id*="code" i]',
        'input[placeholder*="code" i]',
        'input[name*="pin" i]',
        'input[id*="pin" i]',
        'input[placeholder*="pin" i]'
    ]
    return fill_first_available_scopes(page, selectors, code)

def choose_verification_link(links, current_url):
    if not links:
        return None
    host = (urlparse(current_url or "").hostname or "").lower()
    prioritized = []
    fallback = []
    for link in links:
        low = link.lower()
        if any(token in low for token in ["verify", "confirm", "activate", "registration", "account"]):
            prioritized.append(link)
        elif host and host in low:
            prioritized.append(link)
        else:
            fallback.append(link)
    return (prioritized or fallback)[0]

def resolve_email_challenge(page, email_address, wait_seconds):
    if not email_address:
        return False, "missing_email"

    import requests
    sender_filter = infer_sender_filter(page.url)
    verification_url = email_service_url("/email/verification")
    deadline = time.time() + max(5, wait_seconds)

    while time.time() < deadline:
        try:
            response = requests.post(verification_url, json={
                "email_address": email_address,
                "sender_filter": sender_filter,
                "time_range_minutes": 30
            }, timeout=15)
            if response.status_code == 200:
                data = response.json()
                otp_code = data.get("otp_code")
                if otp_code and fill_code_field(page, otp_code):
                    clicked, _ = click_matching_control(page, [
                        r"verify", r"confirm", r"continue", r"submit", r"next"
                    ], skip_final_submit=True)
                    page.wait_for_timeout(4000)
                    return clicked, "otp"

                link = choose_verification_link(data.get("links", []), page.url)
                if link:
                    page.goto(link, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(4000)
                    return True, "link"
        except Exception as err:
            print(f"[Access Gate] Email verification poll skipped: {err}")

        page.wait_for_timeout(5000)

    return False, "email_timeout"

def normalize_discovery_value(label, input_type, value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    label_low = (label or "").lower()
    if "phone" in label_low or "mobile" in label_low or "cell" in label_low or input_type == "tel":
        digits = re.sub(r"\D+", "", text)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) == 10:
            return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return text

US_STATE_ALIASES = {
    "al": "Alabama", "ak": "Alaska", "az": "Arizona", "ar": "Arkansas",
    "ca": "California", "co": "Colorado", "ct": "Connecticut", "de": "Delaware",
    "fl": "Florida", "ga": "Georgia", "hi": "Hawaii", "id": "Idaho",
    "il": "Illinois", "in": "Indiana", "ia": "Iowa", "ks": "Kansas",
    "ky": "Kentucky", "la": "Louisiana", "me": "Maine", "md": "Maryland",
    "ma": "Massachusetts", "mi": "Michigan", "mn": "Minnesota", "ms": "Mississippi",
    "mo": "Missouri", "mt": "Montana", "ne": "Nebraska", "nv": "Nevada",
    "nh": "New Hampshire", "nj": "New Jersey", "nm": "New Mexico", "ny": "New York",
    "nc": "North Carolina", "nd": "North Dakota", "oh": "Ohio", "ok": "Oklahoma",
    "or": "Oregon", "pa": "Pennsylvania", "ri": "Rhode Island", "sc": "South Carolina",
    "sd": "South Dakota", "tn": "Tennessee", "tx": "Texas", "ut": "Utah",
    "vt": "Vermont", "va": "Virginia", "wa": "Washington", "wv": "West Virginia",
    "wi": "Wisconsin", "wy": "Wyoming", "dc": "District of Columbia"
}

def placeholder_discovery_value(field):
    label = (field.get("label") or "").lower()
    input_type = field.get("input_type")
    if field.get("risk") == "high":
        return None
    if input_type in {"password", "file", "checkbox", "radio", "submit", "button", "hidden"}:
        return None
    if "email" in label or input_type == "email":
        return None
    if "phone" in label:
        return None
    if "name" in label:
        return None
    if "zip" in label or "postal" in label:
        return "00000"
    if "city" in label:
        return "Discovery"
    if "state" in label:
        return "California"
    if "country" in label:
        return "United States"
    if "street" in label or "address" in label:
        return "DISCOVERY ONLY"
    return "DISCOVERY ONLY"

def allow_placeholder_autofill_for_page(page, req):
    if not req.allow_placeholder_autofill:
        return False
    url = (getattr(page, "url", "") or "").lower()
    if "localhost" in url or "127.0.0.1" in url:
        return True
    return os.getenv("ALLOW_REAL_ATS_PLACEHOLDER_AUTOFILL", "false").lower() == "true"

def field_key(field):
    return " ".join(str(field.get(key, "")) for key in ["label", "name", "id", "placeholder"]).strip()

def field_requires_user(field, user_data, req, allow_placeholders=None):
    if not field.get("required") or field.get("value_present"):
        return False
    if field.get("input_type") in {"hidden", "submit", "button"}:
        return False
    if field.get("risk") == "high":
        return True
    if candidate_profile_value(field.get("label"), field.get("input_type"), user_data):
        return False
    if allow_placeholders is None:
        allow_placeholders = req.allow_placeholder_autofill
    if allow_placeholders and placeholder_discovery_value(field):
        return False
    return True

def is_safe_discovery_checkbox(field):
    if field.get("input_type") != "checkbox":
        return False
    return bool(SAFE_DISCOVERY_CHECKBOX_RE.search(field_key(field)))

def get_scope_by_index(page, scope_index):
    scopes = get_apply_scopes(page)
    if scope_index is None:
        return scopes[0]
    try:
        return scopes[int(scope_index)]
    except Exception:
        return scopes[0]

def normalized_option_text(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

def quoted_css_attr(value):
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')

def discovery_option_terms(label, value):
    terms = {normalized_option_text(value)}
    label_low = (label or "").lower()
    value_norm = normalized_option_text(value)

    if "state" in label_low or "province" in label_low or "region" in label_low:
        if value_norm in US_STATE_ALIASES:
            terms.add(normalized_option_text(US_STATE_ALIASES[value_norm]))
        for abbr, name in US_STATE_ALIASES.items():
            if value_norm == normalized_option_text(name):
                terms.add(abbr)
                terms.add(normalized_option_text(name))

    if "country" in label_low or "region" in label_low:
        if value_norm in {"united states", "united states of america", "usa", "us", "u s", "u s a"}:
            terms.update({
                "united states",
                "united states of america",
                "usa",
                "us",
                "u s",
                "u s a"
            })

    return {term for term in terms if term}

def choose_matching_option(options, label, value):
    terms = discovery_option_terms(label, value)
    usable = [
        option for option in options
        if option.get("value") and not option.get("disabled") and not re.search(r"select|choose", option.get("text") or "", re.IGNORECASE)
    ]
    for option in usable:
        option_terms = {
            normalized_option_text(option.get("value")),
            normalized_option_text(option.get("text"))
        }
        if terms & option_terms:
            return option
    for option in usable:
        option_value = normalized_option_text(option.get("value"))
        option_text = normalized_option_text(option.get("text"))
        if any(term and (term in option_text or term in option_value or option_text in term or option_value in term) for term in terms):
            return option
    return None

def backing_select_keys(field):
    keys = []
    for raw in [field.get("id"), field.get("name")]:
        token = str(raw or "").strip()
        if not token:
            continue
        if token.startswith("visible-input-"):
            token = token[len("visible-input-"):]
        if token.endswith("-input"):
            token = token[:-len("-input")]
        keys.append(token)
        for match in re.findall(r"[\w:-]*_slt_[\w:-]*", token):
            keys.append(match)
    deduped = []
    seen = set()
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            deduped.append(key)
    return deduped

def select_backing_hidden_select(scope, field, value):
    keys = backing_select_keys(field)
    if not keys and field.get("tag") != "select":
        return False

    candidates = []
    for key in keys:
        escaped = quoted_css_attr(key)
        candidates.extend([
            f'select[id="{escaped}"]',
            f'select[name="{escaped}"]',
            f'select[id*="{escaped}"]',
            f'select[name*="{escaped}"]'
        ])
    if field.get("tag") == "select" and field.get("selector"):
        candidates.insert(0, field.get("selector"))

    for selector in candidates:
        try:
            select = scope.locator(selector).first
            if select.count() == 0:
                continue
            options = select.evaluate("""
                el => Array.from(el.options || [])
                  .map(option => ({ value: option.value, text: option.innerText.trim(), disabled: option.disabled }))
            """)
            choice = choose_matching_option(options, field.get("label"), value)
            if not choice:
                continue
            try:
                select.select_option(value=choice["value"], timeout=2000, force=True)
            except Exception:
                select.evaluate("""
                    (el, value) => {
                      el.value = value;
                      el.dispatchEvent(new Event("input", { bubbles: true }));
                      el.dispatchEvent(new Event("change", { bubbles: true }));
                      if (window.jQuery) window.jQuery(el).trigger("change");
                    }
                """, choice["value"])
            try:
                visible_selector = field.get("selector")
                if visible_selector and field.get("tag") != "select":
                    display_value = choice.get("text") or value
                    scope.locator(visible_selector).first.evaluate("""
                        (el, displayValue) => {
                          el.value = displayValue;
                          el.dispatchEvent(new Event("input", { bubbles: true }));
                          el.dispatchEvent(new Event("change", { bubbles: true }));
                        }
                    """, display_value)
            except Exception:
                pass
            return True
        except Exception as err:
            print(f"[Discovery] Hidden select candidate skipped for '{field.get('label')}': {err}")
    return False

def is_autocomplete_select_field(field):
    text = " ".join(str(field.get(key, "")) for key in ["label", "name", "id", "selector"])
    return bool(re.search(r"(_slt_|visible-input-|combobox|autocomplete)", text, re.IGNORECASE))

def click_matching_autocomplete_option(page, scope, locator, field, value):
    listbox_ids = []
    try:
        aria_owns = locator.get_attribute("aria-owns", timeout=500)
        if aria_owns:
            listbox_ids.extend(aria_owns.split())
    except Exception:
        pass
    field_id = field.get("id") or ""
    if field_id:
        listbox_ids.append(f"{field_id}_listbox")

    selectors = []
    for listbox_id in listbox_ids:
        escaped = quoted_css_attr(listbox_id)
        selectors.extend([
            f'[id="{escaped}"] li',
            f'[id="{escaped}"] [role="option"]'
        ])
    selectors.extend([
        '.ui-autocomplete:visible li',
        '[role="listbox"]:visible [role="option"]'
    ])

    terms = discovery_option_terms(field.get("label"), value)
    for selector in selectors:
        try:
            options = scope.locator(selector)
            for index in range(min(options.count(), 30)):
                option = options.nth(index)
                if not option.is_visible(timeout=500):
                    continue
                option_text = normalized_option_text(locator_label(option))
                if not option_text:
                    continue
                if any(term == option_text or term in option_text or option_text in term for term in terms):
                    option.click(timeout=2000)
                    page.wait_for_timeout(500)
                    return True
        except Exception:
            continue
    return False

def fill_autocomplete_select_field(page, scope, locator, field, value):
    try:
        locator.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass
    try:
        locator.click(timeout=2000)
        locator.fill(value, timeout=3000)
    except Exception:
        return False

    for _ in range(8):
        page.wait_for_timeout(500)
        if select_backing_hidden_select(scope, field, value):
            return True
        if click_matching_autocomplete_option(page, scope, locator, field, value):
            if select_backing_hidden_select(scope, field, value):
                return True
            try:
                current = normalized_option_text(locator.input_value(timeout=500))
                if current in discovery_option_terms(field.get("label"), value):
                    return True
            except Exception:
                pass

    try:
        locator.press("ArrowDown", timeout=1000)
        page.wait_for_timeout(200)
        locator.press("Enter", timeout=1000)
        page.wait_for_timeout(500)
        if select_backing_hidden_select(scope, field, value):
            return True
    except Exception:
        pass

    try:
        current = normalized_option_text(locator.input_value(timeout=500))
        return current in discovery_option_terms(field.get("label"), value)
    except Exception:
        return False

def fill_discovery_field(page, field, value):
    value = normalize_discovery_value(field.get("label"), field.get("input_type"), value)
    if not value:
        return False
    if field.get("input_type") in {"hidden", "submit", "button", "file", "password"}:
        return False

    scope = get_scope_by_index(page, field.get("scope_index"))
    selector = field.get("selector")
    if not selector:
        return False
    try:
        locator = scope.locator(selector).first
        if locator.count() == 0:
            return False
        tag = field.get("tag")
        input_type = field.get("input_type")
        if tag == "select":
            return select_backing_hidden_select(scope, field, value)
        if input_type == "checkbox":
            if not is_safe_discovery_checkbox(field):
                return False
            try:
                if not locator.is_checked(timeout=500):
                    locator.check(timeout=2000, force=True)
                return True
            except Exception:
                locator.click(timeout=2000, force=True)
                return True
        if input_type == "radio":
            return False
        if select_backing_hidden_select(scope, field, value):
            return True
        if is_autocomplete_select_field(field) and fill_autocomplete_select_field(page, scope, locator, field, value):
            return True
        locator.fill(value, timeout=3000)
        return True
    except Exception as err:
        print(f"[Discovery] Could not fill field '{field.get('label')}': {err}")
        return False

def fill_discovery_page_fields(page, fields, user_data, req):
    filled = []
    missing_required = []
    skipped = []
    allow_placeholders = allow_placeholder_autofill_for_page(page, req)

    for field in fields:
        input_type = field.get("input_type")
        if field.get("disabled") or field.get("read_only"):
            continue
        if input_type in {"checkbox", "radio"} and field.get("checked"):
            continue
        if input_type not in {"checkbox", "radio"} and field.get("value_present"):
            continue
        key = field_key(field)
        if field.get("risk") == "high":
            if field.get("required") or input_type in {"radio", "checkbox", "select"}:
                missing_required.append({
                    "field": key,
                    "risk": field.get("risk"),
                    "reason": "high_risk_requires_user_confirmation"
                })
            else:
                skipped.append({"field": key, "reason": "high_risk"})
            continue

        if is_safe_discovery_checkbox(field) and req.allow_low_risk_autofill:
            if fill_discovery_field(page, field, "true"):
                filled.append({
                    "field": key,
                    "source": "safe_navigation_checkbox",
                    "risk": field.get("risk")
                })
                continue

        answer = candidate_profile_value(field.get("label"), field.get("input_type"), user_data)
        source = "profile"
        if not answer and allow_placeholders:
            answer = placeholder_discovery_value(field)
            source = "discovery_placeholder" if answer else None

        if answer and req.allow_low_risk_autofill:
            if fill_discovery_field(page, field, answer):
                filled.append({
                    "field": key,
                    "source": source,
                    "risk": field.get("risk")
                })
                continue

        if field_requires_user(field, user_data, req, allow_placeholders=allow_placeholders):
            missing_required.append({
                "field": key,
                "risk": field.get("risk"),
                "reason": "missing_profile_value"
            })
        else:
            skipped.append({
                "field": key,
                "risk": field.get("risk"),
                "reason": "no_fill_needed_or_no_profile_value"
            })

    return {
        "filled": filled,
        "missing_required": missing_required,
        "skipped": skipped
    }

def page_has_final_submit(page):
    for scope in get_apply_scopes(page):
        for role in ["button", "link"]:
            try:
                locator = scope.get_by_role(role, name=FINAL_SUBMIT_RE).first
                if locator.count() > 0 and locator.is_visible(timeout=500):
                    return True
            except Exception:
                continue
        try:
            controls = scope.locator('button, input[type="submit"], input[type="button"], a')
            for index in range(min(controls.count(), 30)):
                label = locator_label(controls.nth(index))
                if FINAL_SUBMIT_RE.search(label):
                    return True
        except Exception:
            continue
    return False

def click_next_form_step(page):
    return click_matching_control(page, [
        r"\bsave\s*(and|&)\s*continue\b",
        r"\bsave\s*/\s*continue\b",
        r"\bnext\b",
        r"\bcontinue\b",
        r"\bcontinue application\b",
        r"\bproceed\b",
        r"\breview\b"
    ], skip_final_submit=True, avoid_patterns=[
        r"discontinued",
        r"submit application",
        r"send application",
        r"complete application",
        r"finish application",
        r"delete",
        r"cancel",
        r"withdraw"
    ])

def schema_signature(fields):
    keys = []
    for field in fields:
        keys.append("|".join([
            str(field.get("label", "")),
            str(field.get("name", "")),
            str(field.get("id", "")),
            str(field.get("input_type", ""))
        ]))
    return "\n".join(keys)

def discover_application_steps(page, req, user_data):
    pages = []
    all_fields = []
    stop_reason = None
    seen = set()

    for page_number in range(1, max(1, min(req.max_form_pages, 20)) + 1):
        dismiss_popups(page)
        resume_upload = handle_resume_upload_prompt(page, req)
        if resume_upload.get("uploaded"):
            wait_for_apply_page_ready(page, timeout=20000, allow_reload=False)
            dismiss_popups(page)
        fields = extract_form_schema(page, user_data)
        signature = f"{page.url}\n{schema_signature(fields)}"
        if signature in seen:
            stop_reason = "repeated_form_page"
            break
        seen.add(signature)

        stage = infer_apply_stage(page, fields)
        page_state = build_page_state(page, fields, user_data, stage=stage)
        preflight = build_preflight(page, fields, user_data, req, stage=stage)
        fill_result = fill_discovery_page_fields(page, fields, user_data, req)
        page_record = {
            "page_number": page_number,
            "url": page.url,
            "stage": stage,
            "field_count": len(fields),
            "fields": fields,
            "page_state": page_state,
            "preflight": preflight,
            "autofill": fill_result,
            "resume_upload": resume_upload,
        }
        pages.append(page_record)
        all_fields.extend(fields)

        if page_has_final_submit(page):
            stop_reason = "final_submit_guard"
            break

        has_high_risk_missing = any(item.get("risk") == "high" for item in fill_result["missing_required"])
        if has_high_risk_missing:
            stop_reason = "high_risk_required_fields"
            break

        if fill_result["missing_required"]:
            stop_reason = "missing_required_fields"
            break

        clicked, label = click_next_form_step(page)
        if not clicked:
            if req.allow_visual_fallback:
                visual_result = visual_access_step(page, req, user_data, reason="no_next_step_control")
                page_record["visual_fallback"] = visual_result
                if visual_result.get("stop_reason") == "final_submit_guard":
                    stop_reason = "final_submit_guard"
                    break
                if visual_result.get("acted"):
                    page.wait_for_timeout(2000)
                    continue
            stop_reason = "no_next_step_control"
            break
        page_record["next_action"] = f"clicked:{label}"
        page.wait_for_timeout(3000)
        wait_for_apply_page_ready(page, timeout=25000, allow_reload=False)

    return {
        "pages": pages,
        "fields": all_fields,
        "stop_reason": stop_reason or "max_form_pages_reached"
    }

def run_apply_access_state_machine(page, req, user_data):
    account_key, account_meta = build_account_key(req.url, user_data.get("email"))
    account_record = get_registry_account(account_key)
    password, password_source = get_application_password(req, account_record)
    account_known = bool(account_record.get("account_created") or account_record.get("account_exists"))
    pending_account_event = None
    pending_account_password = None
    history = []
    last_fields = []
    last_page_state = None
    last_preflight = None
    blocked_reason = None

    for step in range(max(1, min(req.max_steps, 20))):
        readiness = wait_for_apply_page_ready(page, timeout=25000, allow_reload=False)
        dismiss_popups(page)
        context_url = page.url if page.url and page.url != "about:blank" else req.url
        current_key, current_meta = build_account_key(context_url, user_data.get("email"))
        if current_key != account_key:
            account_key = current_key
            account_meta = current_meta
            account_record = get_registry_account(account_key)
            password, password_source = get_application_password(req, account_record)
            account_known = bool(account_record.get("account_created") or account_record.get("account_exists"))
        fields = extract_form_schema(page, user_data)
        last_fields = fields
        stage = infer_apply_stage(page, fields)
        last_page_state = build_page_state(page, fields, user_data, stage=stage)
        last_preflight = build_preflight(page, fields, user_data, req, stage=stage)
        history.append({
            "step": step + 1,
            "stage": stage,
            "url": page.url,
            "field_count": len(fields),
            "readiness": readiness,
            "preflight": {
                "page_status": last_preflight.get("page_status"),
                "required_fields_count": last_preflight.get("required_fields_count"),
                "auto_fillable_count": last_preflight.get("auto_fillable_count"),
                "needs_user_count": last_preflight.get("needs_user_count"),
                "blocking_issue_types": [
                    item.get("type") for item in last_preflight.get("blocking_issues", [])
                ],
                "final_submit_visible": last_preflight.get("final_submit_visible"),
            }
        })
        history[-1]["account_context"] = sanitized_account_status(account_key, account_record)

        if pending_account_event and stage not in {"sign_in", "create_account", "privacy_policy", "email_verification", "blocked_captcha"}:
            account_record = remember_apply_account(
                account_key,
                account_meta,
                password=pending_account_password,
                event=pending_account_event
            )
            account_known = True
            pending_account_event = None
            pending_account_password = None

        if account_known and is_post_auth_job_listing(page, req.url):
            history[-1]["action"] = "returned_to_original_job_after_auth"
            page.goto(req.url, wait_until="domcontentloaded", timeout=45000)
            wait_for_apply_page_ready(page, timeout=45000, allow_reload=True)
            continue

        if stage == "application_form":
            account_record = remember_apply_account(account_key, account_meta, event="form_access")
            account_known = bool(account_record.get("account_created") or account_record.get("account_exists"))
            discovery = None
            result_fields = fields
            if req.discover_all_steps:
                discovery = discover_application_steps(page, req, user_data)
                result_fields = discovery.get("fields", fields)
            return {
                "success": True,
                "stage": stage,
                "blocked_reason": None,
                "fields": result_fields,
                "history": history,
                "account": sanitized_account_status(account_key, account_record),
                "discovery": discovery,
                "page_state": last_page_state,
                "preflight": last_preflight,
            }

        if stage == "blocked_captcha":
            blocked_reason = "captcha_or_bot_challenge"
            break

        if stage == "privacy_policy":
            if not req.allow_terms_acceptance:
                blocked_reason = "terms_confirmation_required"
                break
            clicked_agree, label = click_matching_control(page, [
                r"agree", r"accept"
            ], skip_final_submit=True, avoid_patterns=[
                r"disagree", r"decline"
            ])
            if clicked_agree:
                history[-1]["action"] = f"clicked:{label}"
                continue
            blocked_reason = "privacy_policy_no_agree_button"
            break

        if stage == "email_verification":
            if not req.allow_email_verification:
                blocked_reason = "email_verification_disabled"
                break
            sent_email, send_label = click_matching_control(page, [
                r"^send email$", r"send email", r"send link", r"email me"
            ], skip_final_submit=True)
            if sent_email:
                history[-1]["action"] = f"clicked:{send_label}"
                account_record = remember_apply_account(account_key, account_meta, event="guest_email_sent")
                page.wait_for_timeout(4000)
                clicked_ok, ok_label = click_matching_control(page, [
                    r"^ok$", r"^close$", r"^done$"
                ], skip_final_submit=True)
                if clicked_ok:
                    history[-1]["email_sent_acknowledged"] = f"clicked:{ok_label}"
                    page.wait_for_timeout(2000)
            solved, method = resolve_email_challenge(page, user_data.get("email"), req.wait_for_email_seconds)
            history[-1]["email_verification_method"] = method
            if not solved:
                blocked_reason = method
                break
            account_record = remember_apply_account(account_key, account_meta, event="email_verification_solved")
            continue

        if stage == "sign_in":
            attempted_login = any(
                item.get("stage") == "sign_in" and "login_attempt" in item
                for item in history[:-1]
            )
            attempted_create = any(
                item.get("stage") == "create_account" and item.get("account_creation_attempt")
                for item in history[:-1]
            )
            attempted_guest = any(item.get("guest_apply_attempt") for item in history[:-1])
            if attempted_guest and not attempted_login:
                fill_auth_identity(page, user_data, password)
                clicked_continue, label = click_matching_control(page, [
                    r"^continue$", r"^next$"
                ], skip_final_submit=True, avoid_patterns=[
                    r"cancel", r"back"
                ])
                if clicked_continue:
                    history[-1]["guest_email_attempt"] = True
                    history[-1]["action"] = f"clicked:{label}"
                    continue
            if (account_known or attempted_create) and not attempted_login:
                login_password, password_adjusted = adapt_password_to_visible_policy(
                    page, password, enabled=req.adjust_password_to_policy
                )
                fill_auth_identity(page, user_data, login_password)
                clicked_login, label = click_matching_control(page, [
                    r"^sign in$", r"^log in$", r"^login$"
                ], skip_final_submit=True, avoid_patterns=[
                    r"linkedin", r"facebook", r"google", r"single sign", r"sso"
                ])
                if clicked_login:
                    history[-1]["login_attempt"] = True
                    history[-1]["password_policy_adjusted"] = password_adjusted
                    history[-1]["password_source"] = password_source
                    history[-1]["action"] = f"clicked:{label}"
                    pending_account_event = "login"
                    pending_account_password = login_password
                    continue

            if not account_known:
                clicked_create, label = click_matching_control(page, [
                    r"create account", r"create profile", r"new user", r"register", r"sign up",
                    r"don'?t have an account", r"no account", r"create one"
                ], skip_final_submit=True)
                if clicked_create:
                    history[-1]["action"] = f"clicked:{label}"
                    continue
            blocked_reason = "sign_in_no_action"
            break

        if stage == "create_account":
            create_text = page_body_text(page).lower()
            attempted_login = any(
                item.get("stage") in {"sign_in", "create_account"} and "login_attempt" in item
                for item in history[:-1]
            )
            if account_known and not attempted_login:
                login_password, password_adjusted = adapt_password_to_visible_policy(
                    page, password, enabled=req.adjust_password_to_policy
                )
                fill_auth_identity(page, user_data, login_password)
                clicked_login, label = click_matching_control(page, [
                    r"^sign in$", r"^log in$", r"^login$", r"continue", r"next"
                ], skip_final_submit=True, avoid_patterns=[
                    r"linkedin", r"facebook", r"google", r"single sign", r"sso",
                    r"new user", r"register", r"sign up", r"guest"
                ])
                if clicked_login:
                    history[-1]["login_attempt"] = True
                    history[-1]["password_policy_adjusted"] = password_adjusted
                    history[-1]["password_source"] = password_source
                    history[-1]["action"] = f"clicked:{label}"
                    pending_account_event = "login"
                    pending_account_password = login_password
                    continue
            account_exists_warning = any(token in create_text for token in [
                "created an account in the past",
                "account already exists",
                "already exists",
                "email already in use",
                "email address is already registered",
                "there is already an account",
                "you already have an account",
            ])
            if account_exists_warning:
                account_record = remember_apply_account(account_key, account_meta, event="exists_warning")
                account_known = True
                clicked_sign_in, label = click_matching_control(page, [
                    r"sign in", r"log in", r"login"
                ], skip_final_submit=True, avoid_patterns=[
                    r"linkedin", r"facebook", r"google", r"single sign", r"sso"
                ])
                if clicked_sign_in:
                    history[-1]["action"] = f"clicked:{label}"
                    continue
                blocked_reason = "account_exists_but_no_sign_in_action"
                break
            clicked_guest, label = click_matching_control(page, [
                r"apply as guest", r"continue as guest", r"guest apply"
            ], skip_final_submit=True)
            if clicked_guest:
                history[-1]["action"] = f"clicked:{label}"
                history[-1]["guest_apply_attempt"] = True
                account_record = remember_apply_account(account_key, account_meta, event="guest_apply")
                continue
            if not req.allow_account_creation:
                blocked_reason = "account_creation_disabled"
                break
            if terms_checkbox_count(page) and not req.allow_terms_acceptance:
                blocked_reason = "terms_confirmation_required"
                break
            account_password, password_adjusted = adapt_password_to_visible_policy(
                page, password, enabled=req.adjust_password_to_policy
            )
            fill_auth_identity(page, user_data, account_password)
            security_result = fill_security_questions(page, req, user_data)
            history[-1]["password_policy_adjusted"] = password_adjusted
            history[-1]["security_questions"] = security_result
            history[-1]["account_creation_attempt"] = True
            if req.allow_terms_acceptance:
                click_terms_checkboxes(page)
            clicked_register, label = click_matching_control(page, [
                r"create account", r"create profile", r"new user", r"register", r"sign up", r"continue", r"next"
            ], skip_final_submit=True)
            if clicked_register:
                history[-1]["action"] = f"clicked:{label}"
                pending_account_event = "created"
                pending_account_password = account_password
                continue
            blocked_reason = "create_account_no_action"
            break

        if stage in {"job_detail", "unknown"}:
            prefer_resume = bool(req.allow_resume_upload and req.resume_path and os.path.exists(req.resume_path))
            clicked_workday_choice, workday_choice = click_workday_application_choice(page, prefer_resume=prefer_resume)
            if clicked_workday_choice:
                history[-1]["action"] = f"clicked:{workday_choice}"
                continue
            if fields:
                account_record = remember_apply_account(account_key, account_meta, event="form_access")
                discovery = None
                result_fields = fields
                if req.discover_all_steps:
                    discovery = discover_application_steps(page, req, user_data)
                    result_fields = discovery.get("fields", fields)
                return {
                    "success": True,
                    "stage": "application_form",
                    "blocked_reason": None,
                    "fields": result_fields,
                    "history": history,
                    "account": sanitized_account_status(account_key, account_record),
                    "discovery": discovery,
                    "page_state": last_page_state,
                    "preflight": last_preflight,
                }
            if req.allow_visual_fallback:
                visual_result = visual_access_step(page, req, user_data, reason=f"{stage}_navigation")
                history[-1]["visual_fallback"] = visual_result
                if visual_result.get("stop_reason") in {"final_submit_guard", "human_required", "captcha", "blocked", "no_safe_action"}:
                    blocked_reason = visual_result.get("stop_reason")
                    break
                if visual_result.get("acted"):
                    continue
            clicked_apply, label = click_matching_control(page, [
                r"apply manually", r"autofill with resume", r"use my last application",
                r"let'?s get started", r"get started", r"apply now", r"apply to job", r"apply",
                r"start application", r"begin application", r"continue applying", r"continue", r"next"
            ], skip_final_submit=True)
            if clicked_apply:
                history[-1]["action"] = f"clicked:{label}"
                continue
            blocked_reason = "no_action_found"
            break

    return {
        "success": False,
        "stage": history[-1]["stage"] if history else "unknown",
        "blocked_reason": blocked_reason or "max_steps_reached",
        "fields": last_fields,
        "history": history,
        "account": sanitized_account_status(account_key, account_record),
        "discovery": None,
        "page_state": last_page_state,
        "preflight": last_preflight,
    }

def ensure_linkedin_logged_in(page):
    """
    Check if the user is logged into LinkedIn in the persistent context.
    If not, navigate to login and wait for manual user interaction (up to 90 seconds).
    """
    try:
        # Load credentials from scheduler_config.json if available
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "scheduler_config.json")
        username = ""
        password = ""
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config_data = json.load(f)
                    username = config_data.get("linkedin_username", "")
                    password = config_data.get("linkedin_password", "")
            except Exception as read_err:
                print(f"Failed to read scheduler_config.json for login credentials: {read_err}")

        print("Checking LinkedIn login session status...")
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        
        # Check if the page is currently a login page or feed
        current_url = page.url
        
        # A robust way is to check if we are redirected to feed/home, or if we have a global navigation bar
        is_logged_in = ("feed" in current_url or page.locator("#global-nav").count() > 0) and "login" not in current_url and "checkpoint" not in current_url
        
        if not is_logged_in:
            print("\n[LinkedIn Login Needed] Local persistent session is logged out or expired!")
            print("Navigating to login page...")
            page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=30000)
            
            # Try to auto-fill username and password if configured
            if username and password:
                login_success = False
                
                # 1. Try Vision-based login first
                if client:
                    login_success = vision_login_linkedin(page, username, password)
                
                # 2. Selector-based login fallback
                if not login_success:
                    print("Vision login skipped or failed. Falling back to selector-based login...")
                    try:
                        # Log page URL and title before attempting
                        print(f"Current login page URL: {page.url}, Title: {page.title()}")
                        
                        # Wait for form elements (using broader selectors, filtered by visibility to avoid honeypots)
                        login_selector = 'input#username:visible, input[name="session_key"]:visible, input[autocomplete="username"]:visible, input[type="email"]:visible, input[type="text"]:visible'
                        page.wait_for_selector(login_selector, timeout=5000)
                        
                        # Fill username
                        username_input = page.locator(login_selector).first
                        username_input.fill(username)
                        page.wait_for_timeout(500)
                        
                        # Fill password
                        password_selector = 'input#password:visible, input[name="session_password"]:visible, input[autocomplete="current-password"]:visible, input[type="password"]:visible'
                        password_input = page.locator(password_selector).first
                        password_input.fill(password)
                        page.wait_for_timeout(500)
                        
                        # Click sign in
                        submit_button = page.locator('button[type="submit"], button[aria-label="Sign in"]')
                        submit_button.click()
                        print("Credentials submitted. Waiting for page transition...")
                        page.wait_for_timeout(3000)
                    except Exception as autofill_err:
                        print(f"Autofill or form submission skipped/failed: {autofill_err}")
                        print(f"Current page URL after attempt: {page.url}, Title: {page.title()}")
                
                # Check if verification code page is loaded
                current_url = page.url
                pin_selector = 'input#input-pin, input#email-pin, input[name="pin"], input[autocomplete="one-time-code"], input[id*="pin"], input[id*="code"]'
                is_challenge_page = "checkpoint" in current_url or "challenge" in current_url or page.locator(pin_selector).count() > 0
                
                if is_challenge_page:
                    print("[LinkedIn Challenge] Verification PIN code screen detected.")
                    challenge_solved = False
                    
                    # 1. Try Vision-based challenge solver first
                    if client:
                        challenge_solved = vision_solve_challenge_linkedin(page, username)
                        
                    # 2. Selector-based challenge solver fallback
                    if not challenge_solved:
                        print("Vision challenge solver skipped or failed. Falling back to selector-based...")
                        # Wait for the email to arrive
                        print("Waiting 10 seconds for verification email to arrive in inbox...")
                        page.wait_for_timeout(10000)
                        
                        # Request OTP from local email server
                        email_otp_url = email_service_url("/email/otp")
                        print(f"Requesting OTP from email server at {email_otp_url}...")
                        
                        try:
                            import requests
                            resp = requests.post(email_otp_url, json={
                                "email_address": username,
                                "sender_filter": "linkedin.com"
                            }, timeout=15)
                            
                            if resp.status_code == 200:
                                otp_data = resp.json()
                                otp_code = otp_data.get("otp_code")
                                if otp_code:
                                    print(f"SUCCESS: Retrieved OTP code '{otp_code}' from email. Autofilling...")
                                    pin_input = page.locator(pin_selector).first
                                    pin_input.fill(otp_code)
                                    page.wait_for_timeout(1000)
                                    
                                    # Submit PIN
                                    submit_pin_selector = 'button#email-pin-submit-button, button[type="submit"], button#action-btn, button[aria-label="Submit"]'
                                    submit_btn = page.locator(submit_pin_selector).first
                                    submit_btn.click()
                                    print("OTP submitted. Waiting for transition...")
                                    page.wait_for_timeout(5000)
                                else:
                                    print("No OTP code returned by the email server.")
                            else:
                                print(f"Email server returned status {resp.status_code}: {resp.text}")
                        except Exception as otp_err:
                            print(f"Failed to fetch OTP from email server: {otp_err}")
            else:
                print("No stored LinkedIn credentials found in scheduler_config.json. Manual entry required.")
                
            print("\n======================================================================")
            print("WARNING: PLEASE LOG IN MANUALLY IN THE OPENED CHROME WINDOW NOW!")
            print("  - Complete your Username and Password.")
            print("  - Solve any CAPTCHAs or enter security verification codes.")
            print("  - The bot will wait and poll every 2 seconds for your login success.")
            print("======================================================================\n")
            
            # Poll every 2 seconds for up to 90 seconds (45 attempts)
            logged_in_success = False
            for attempt in range(45):
                page.wait_for_timeout(2000)
                try:
                    curr_url = page.url
                    curr_html = page.content()
                    # If we are successfully redirected to feed or home, and sign in buttons are gone
                    if "feed" in curr_url or ("login" not in curr_url and "challenge" not in curr_url and "Sign in" not in curr_html and "Join now" not in curr_html):
                        print("SUCCESS: LinkedIn Login Detected! Proceeding...")
                        logged_in_success = True
                        break
                except Exception as poll_err:
                    # Browser might be temporarily navigating
                    pass
                    
            if not logged_in_success:
                print("ERROR: Manual login verification timed out after 90 seconds.")
                return False
        else:
            print("SUCCESS: Active local session detected. Direct login bypass successful!")
            
        return True
    except Exception as e:
        print(f"Error checking or verifying login status: {e}")
        return False


app = FastAPI(title="Playwright Vision-Based Automation Server", version="1.0.0")

@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "playwright-automation",
        "headless": (os.getenv("PLAYWRIGHT_HEADLESS") or "true").strip().lower(),
        "gemini_configured": bool(client),
        "email_url_configured": bool(os.getenv("EMAIL_URL")),
    }

class ApplyRequest(BaseModel):
    url: str
    resume_path: str
    user_data: dict
    cookies: list = []

class AccessApplyRequest(BaseModel):
    url: str
    user_data: dict = {}
    resume_path: Optional[str] = None
    application_password: Optional[str] = None
    security_answers: Optional[List[str]] = None
    cookies: list = []
    max_steps: int = 10
    wait_for_email_seconds: int = 60
    allow_account_creation: bool = True
    allow_email_verification: bool = True
    allow_terms_acceptance: bool = False
    allow_security_question_autofill: bool = True
    adjust_password_to_policy: bool = True
    stop_at_form: bool = True
    discover_all_steps: bool = False
    max_form_pages: int = 8
    allow_low_risk_autofill: bool = True
    allow_placeholder_autofill: bool = False
    allow_visual_fallback: bool = True
    allow_resume_upload: bool = True

def compact_text(text):
    return "\n".join(line.strip() for line in (text or "").splitlines() if line.strip())

def trim_linkedin_jd_text(text):
    text = compact_text(text)
    if not text:
        return ""

    start_markers = [
        "About the job",
        "About this job",
        "Job description",
        "What To Expect",
        "Responsibilities",
        "The Role"
    ]
    lowered = text.lower()
    starts = [lowered.find(marker.lower()) for marker in start_markers if lowered.find(marker.lower()) >= 0]
    if starts:
        text = text[min(starts):]

    lowered = text.lower()
    end_markers = [
        "benefits found in job post",
        "job search faster with premium",
        "about the company",
        "interested in working with us",
        "similar jobs",
        "people also viewed",
        "show more\ninterested"
    ]
    ends = [lowered.find(marker.lower()) for marker in end_markers if lowered.find(marker.lower()) > 200]
    if ends:
        text = text[:min(ends)]

    noisy_lines = {
        "share",
        "show more options",
        "send feedback",
        "report this job",
        "apply",
        "save",
        "show more"
    }
    kept = []
    for line in text.splitlines():
        clean = line.strip()
        low = clean.lower()
        if not clean:
            continue
        if low in noisy_lines:
            continue
        if "try premium" in low or "access exclusive applicant insights" in low:
            continue
        if low.startswith("save ") and " at " in low:
            continue
        kept.append(clean)
    return "\n".join(kept).strip()

def clean_location_text(text):
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for line in lines:
        low = line.lower()
        if "responses managed" in low:
            continue
        if low in {"apply", "save"}:
            continue
        for separator in ["·", "路"]:
            if separator in line:
                return line.split(separator, 1)[0].strip()
        return line
    return compact_text(text)

def first_locator_text(scope, selectors, timeout=1000):
    for selector in selectors:
        try:
            locator = scope.locator(selector).first
            if locator.count() == 0:
                continue
            text = compact_text(locator.inner_text(timeout=timeout))
            if text:
                return text
        except Exception:
            continue
    return ""

def first_locator_attr(scope, selectors, attr, timeout=1000):
    for selector in selectors:
        try:
            locator = scope.locator(selector).first
            if locator.count() == 0:
                continue
            value = locator.get_attribute(attr, timeout=timeout)
            if value:
                return value
        except Exception:
            continue
    return ""

def extract_job_id_from_url(url):
    if not url:
        return ""
    patterns = [
        r"currentJobId=(\d+)",
        r"/jobs/view/(\d+)",
        r"/jobs/collections/recommended/\?currentJobId=(\d+)"
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return ""

def save_extracted_jd(idx, company, role, location, apply_link, job_description):
    jd_dir = os.path.join(os.path.dirname(__file__), "..", "data", "extracted_jds")
    os.makedirs(jd_dir, exist_ok=True)
    clean_company = "".join(c for c in (company or "Unknown") if c.isalnum() or c in (" ", "-", "_")).strip() or "Unknown"
    clean_role = "".join(c for c in (role or "Unknown") if c.isalnum() or c in (" ", "-", "_")).strip() or "Unknown"
    filename = f"jd_{idx+1}_{clean_company}_{clean_role}.txt".replace(" ", "_")[:180]
    jd_file_path = os.path.join(jd_dir, filename)
    with open(jd_file_path, "w", encoding="utf-8") as f:
        f.write(f"Company: {company}\n")
        f.write(f"Role: {role}\n")
        f.write(f"Location: {location}\n")
        f.write(f"Apply Link: {apply_link}\n")
        f.write("=" * 40 + "\n")
        f.write(job_description or "")
    return jd_file_path

def parse_card_text(card_text):
    lines = [line.strip() for line in (card_text or "").splitlines() if line.strip()]
    title = lines[0] if len(lines) > 0 else "Unknown"
    company = lines[1] if len(lines) > 1 else "Unknown"
    location = lines[2] if len(lines) > 2 else "Unknown"
    return title, company, location

def get_apply_button(page):
    selectors = [
        ".jobs-apply-button:visible",
        "button.jobs-apply-button:visible",
        "a.jobs-apply-button:visible",
        "button:has-text(\"Apply\")",
        "a:has-text(\"Apply\")"
    ]
    for selector in selectors:
        try:
            count = page.locator(selector).count()
            for i in range(min(count, 5)):
                button = page.locator(selector).nth(i)
                text = compact_text(button.inner_text(timeout=1000)).lower()
                if not text:
                    continue
                if "easy apply" in text:
                    continue
                if "apply" in text:
                    return button, text
        except Exception:
            continue
    return None, ""

def click_continue_if_needed(page):
    selectors = [
        "button:has-text(\"Continue\")",
        "a:has-text(\"Continue\")",
        "button:has-text(\"Continue applying\")",
        "a:has-text(\"Continue applying\")",
        "button:has-text(\"Apply\")",
        "a:has-text(\"Apply\")"
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible(timeout=1000):
                locator.click(timeout=3000)
                page.wait_for_timeout(2500)
                return True
        except Exception:
            continue
    return False

def extract_external_apply_link(page, context, search_url):
    apply_button, apply_text = get_apply_button(page)
    if not apply_button:
        print("[LinkedIn DOM] No external Apply button found; skipping job.")
        return ""
    if "easy apply" in apply_text:
        print("[LinkedIn DOM] Easy Apply detected; skipping job because no external apply URL.")
        return ""

    original_pages = list(context.pages)
    target_page = page
    try:
        try:
            with context.expect_page(timeout=5000) as page_info:
                apply_button.click(timeout=3000)
            target_page = page_info.value
            target_page.wait_for_load_state("domcontentloaded", timeout=10000)
        except PlaywrightTimeoutError:
            apply_button.click(timeout=3000)
            page.wait_for_timeout(3000)
            target_page = page
            for candidate in context.pages:
                if candidate not in original_pages and candidate != page:
                    target_page = candidate
                    break

        for _ in range(3):
            current_url = target_page.url
            if is_external_nonblank_url(current_url):
                return current_url
            if not click_continue_if_needed(target_page):
                target_page.wait_for_timeout(1500)

        current_url = target_page.url
        if is_external_nonblank_url(current_url):
            return current_url
        print(f"[LinkedIn DOM] Apply click did not reach external URL. Current URL: {current_url}")
        return ""
    finally:
        for candidate in list(context.pages):
            if candidate != page and candidate not in original_pages:
                try:
                    candidate.close()
                except Exception:
                    pass
        try:
            if page.url != search_url and not is_linkedin_jobs_search_url(page.url):
                page.goto(search_url)
                page.wait_for_timeout(3000)
        except Exception:
            pass

def extract_detail_text(page):
    for more_selector in [
        "button:has-text(\"Show more\")",
        "button:has-text(\"See more\")",
        "button:has-text(\"Show more results\")"
    ]:
        try:
            button = page.locator(more_selector).first
            if button.count() > 0 and button.is_visible(timeout=1000):
                button.click(timeout=2000)
                page.wait_for_timeout(1000)
        except Exception:
            continue

    selectors = [
        ".jobs-description",
        ".jobs-box__html-content",
        ".jobs-description-content__text",
        ".show-more-less-html__markup",
        ".jobs-search__job-details--container",
        ".jobs-details",
        ".jobs-details__main-content",
        ".job-view-layout",
        ".two-pane-serp-page__detail-view"
    ]
    best = ""
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() == 0:
                continue
            text = compact_text(locator.inner_text(timeout=3000))
            if len(text) > len(best):
                best = text
        except Exception:
            continue
    return trim_linkedin_jd_text(best)

def extract_linkedin_jobs_dom(page, context, search_url, max_jobs=3):
    card_selector = ""
    for selector in [
        "li[data-occludable-job-id]",
        ".jobs-search-results__list-item",
        ".job-card-container",
        ".jobs-search__results-list li",
        ".job-search-card"
    ]:
        try:
            count = page.locator(selector).count()
            if count > 0:
                card_selector = selector
                break
        except Exception:
            continue

    if not card_selector:
        print("[LinkedIn DOM] No job cards found.")
        return []

    count = min(page.locator(card_selector).count(), max_jobs * 3)
    results = []
    seen = set()

    for card_index in range(count):
        if len(results) >= max_jobs:
            break
        card = page.locator(card_selector).nth(card_index)
        try:
            card.scroll_into_view_if_needed(timeout=3000)
            card_text = compact_text(card.inner_text(timeout=3000))
            parsed_title, parsed_company, parsed_location = parse_card_text(card_text)
            title = first_locator_text(card, [
                ".job-card-list__title",
                ".job-card-list__title--link",
                "a.job-card-container__link",
                ".base-search-card__title"
            ]) or parsed_title
            company = first_locator_text(card, [
                ".artdeco-entity-lockup__subtitle",
                ".job-card-container__primary-description",
                ".base-search-card__subtitle"
            ]) or parsed_company
            location = first_locator_text(card, [
                ".artdeco-entity-lockup__caption",
                ".job-card-container__metadata-item",
                ".job-search-card__location"
            ]) or parsed_location
            href = first_locator_attr(card, [
                "a[href*='/jobs/view/']",
                "a[href*='currentJobId']",
                "a.job-card-container__link"
            ], "href")
            linkedin_link = urljoin("https://www.linkedin.com", href) if href else page.url

            card.click(timeout=5000)
            page.wait_for_timeout(2500)
            dismiss_popups(page)

            detail_title = first_locator_text(page, [
                ".jobs-unified-top-card__job-title",
                ".top-card-layout__title",
                "h1"
            ])
            detail_company = first_locator_text(page, [
                ".jobs-unified-top-card__company-name",
                ".topcard__org-name-link",
                ".top-card-layout__card .topcard__flavor"
            ])
            detail_location = first_locator_text(page, [
                ".jobs-unified-top-card__primary-description-container",
                ".topcard__flavor--bullet",
                ".job-details-jobs-unified-top-card__primary-description-container"
            ])

            title = detail_title or title
            company = detail_company or company
            location = clean_location_text(detail_location) or clean_location_text(location)
            job_description = extract_detail_text(page)
            apply_link = extract_external_apply_link(page, context, search_url)

            if not apply_link:
                continue
            if not job_description or len(job_description) < 120:
                print(f"[LinkedIn DOM] JD too short for {title} @ {company}; skipping.")
                continue

            job_id = extract_job_id_from_url(linkedin_link) or extract_job_id_from_url(page.url) or f"linkedin-{int(time.time())}-{card_index}"
            dedupe_key = (job_id, apply_link)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            jd_file_path = save_extracted_jd(len(results), company, title, location, apply_link, job_description)
            print(f"[LinkedIn DOM] Saved extracted JD: {jd_file_path}")
            results.append({
                "job_id": f"linkedin-{job_id}",
                "company": company,
                "role": title,
                "job_description": job_description,
                "location": location,
                "apply_link": apply_link,
                "linkedin_link": linkedin_link,
                "jd_file_path": jd_file_path
            })
        except Exception as dom_err:
            print(f"[LinkedIn DOM] Failed to process card {card_index}: {dom_err}")
            continue

    return results

# Gemini client is initialized at the top of the file

# HTML for a built-in mock form to enable local testing
MOCK_FORM_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Mock Job Application Portal</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600&display=swap" rel="stylesheet">
    <style>
        body {
            font-family: 'Inter', sans-serif;
            background: linear-gradient(135deg, #0f172a, #1e1b4b);
            color: #f8fafc;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
        }
        .container {
            background: rgba(255, 255, 255, 0.05);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.1);
            padding: 40px;
            border-radius: 16px;
            width: 450px;
            box-shadow: 0 10px 25px rgba(0, 0, 0, 0.3);
        }
        h2 {
            margin-top: 0;
            color: #6366f1;
            font-weight: 600;
        }
        .form-group {
            margin-bottom: 20px;
        }
        label {
            display: block;
            margin-bottom: 8px;
            font-size: 14px;
            color: #cbd5e1;
        }
        input[type="text"], input[type="email"] {
            width: 100%;
            padding: 12px;
            border-radius: 8px;
            border: 1px solid rgba(255, 255, 255, 0.2);
            background: rgba(0, 0, 0, 0.2);
            color: white;
            box-sizing: border-box;
            font-size: 14px;
        }
        input:focus {
            outline: none;
            border-color: #6366f1;
        }
        input[type="file"] {
            display: block;
            margin-top: 5px;
            color: #94a3b8;
        }
        button {
            width: 100%;
            background: #6366f1;
            color: white;
            padding: 14px;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: background 0.3s;
        }
        button:hover {
            background: #4f46e5;
        }
        .success-message {
            text-align: center;
            color: #10b981;
            font-size: 18px;
            display: none;
        }
    </style>
</head>
<body>
    <div class="container">
        <div id="form-content">
            <h2>Apply for Software Engineer</h2>
            <p style="color: #94a3b8; font-size: 14px; margin-bottom: 30px;">Fill out your details to submit your application.</p>
            <form id="job-form" onsubmit="event.preventDefault(); submitForm();">
                <div class="form-group">
                    <label for="first_name">First Name</label>
                    <input type="text" id="first_name" name="first_name" placeholder="John" required>
                </div>
                <div class="form-group">
                    <label for="last_name">Last Name</label>
                    <input type="text" id="last_name" name="last_name" placeholder="Doe" required>
                </div>
                <div class="form-group">
                    <label for="email">Email Address</label>
                    <input type="email" id="email" name="email" placeholder="john.doe@example.com" required>
                </div>
                <div class="form-group">
                    <label for="phone">Phone Number</label>
                    <input type="text" id="phone" name="phone" placeholder="+1 (555) 019-2834" required>
                </div>
                <div class="form-group">
                    <label for="resume">Upload PDF Resume</label>
                    <input type="file" id="resume" name="resume" accept=".pdf" required>
                </div>
                <button type="submit" id="submit_btn">Submit Application</button>
            </form>
        </div>
        <div id="success-content" class="success-message">
            <h3>🎉 Success!</h3>
            <p>Application Submitted Successfully!</p>
        </div>
    </div>

    <script>
        function submitForm() {
            document.getElementById('form-content').style.display = 'none';
            document.getElementById('success-content').style.display = 'block';
        }
    </script>
</body>
</html>
"""

@app.get("/mock-form", response_class=HTMLResponse)
def serve_mock_form():
    return MOCK_FORM_HTML

@app.post("/access-apply-form")
def access_apply_form(req: AccessApplyRequest):
    default_user_data = load_default_user_data()
    user_data = {**default_user_data, **(req.user_data or {})}
    if not user_data.get("email"):
        raise HTTPException(status_code=400, detail="user_data.email is required for apply access gates")

    headless = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() == "true"
    print(f"[Access Gate] Launching browser for ATS access flow (headless={headless})...")

    with sync_playwright() as p:
        cookies_to_add = req.cookies if req.cookies else []
        if not cookies_to_add:
            cookies_file_path = os.path.join(os.path.dirname(__file__), "..", "data", "cookies.json")
            if os.path.exists(cookies_file_path):
                try:
                    with open(cookies_file_path, "r", encoding="utf-8") as f:
                        cookies_to_add = json.load(f)
                except Exception as cookie_err:
                    print(f"[Access Gate] Failed to read cookies.json: {cookie_err}")

        timezone, locale = extract_timezone_and_locale(cookies_to_add)
        browser, is_persistent = launch_browser(p, headless, locale=locale, timezone=timezone)
        if is_persistent:
            context = browser
            page = context.pages[0] if context.pages else context.new_page()
        else:
            user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=user_agent,
                locale=locale,
                timezone_id=timezone
            )
            if cookies_to_add:
                try:
                    context.add_cookies(sanitize_cookies(cookies_to_add))
                except Exception as cookie_err:
                    print(f"[Access Gate] Cookie injection skipped: {cookie_err}")
            page = context.new_page()

        try:
            page.goto(req.url, wait_until="domcontentloaded", timeout=45000)
            initial_readiness = wait_for_apply_page_ready(page, timeout=90000, allow_reload=True)
            result = run_apply_access_state_machine(page, req, user_data)
            current_fields = extract_form_schema(page, user_data)
            current_stage = infer_apply_stage(page, current_fields)
            current_page_state = build_page_state(page, current_fields, user_data, stage=current_stage)
            current_preflight = build_preflight(page, current_fields, user_data, req, stage=current_stage)

            screenshot_dir = os.path.join(os.path.dirname(__file__), "..", "screenshots")
            os.makedirs(screenshot_dir, exist_ok=True)
            screenshot_path = os.path.join(screenshot_dir, f"apply_access_{int(time.time())}.png")
            try:
                page.screenshot(path=screenshot_path, full_page=True)
            except Exception as screenshot_err:
                print(f"[Access Gate] Screenshot failed: {screenshot_err}")
                screenshot_path = ""

            return {
                "success": result.get("success", False),
                "status": "form_detected" if result.get("success") else "blocked_or_incomplete",
                "ats": infer_ats(page.url or req.url),
                "original_url": req.url,
                "current_url": page.url,
                "stage": result.get("stage"),
                "blocked_reason": result.get("blocked_reason"),
                "fields": result.get("fields", []),
                "history": result.get("history", []),
                "account": result.get("account", {}),
                "discovery": result.get("discovery"),
                "page_state": result.get("page_state") or current_page_state,
                "preflight": result.get("preflight") or current_preflight,
                "current_page_state": current_page_state,
                "current_preflight": current_preflight,
                "screenshot_path": screenshot_path,
                "stop_at_form": req.stop_at_form,
                "initial_readiness": initial_readiness
            }
        except Exception as err:
            print(f"[Access Gate] Execution error: {err}")
            raise HTTPException(status_code=500, detail=f"Access gate flow failed: {err}")
        finally:
            browser.close()

@app.post("/apply")
def run_apply_loop(req: ApplyRequest):
    if not client:
        raise HTTPException(
            status_code=500,
            detail="Gemini API Key is not configured. Visual Playwright automation requires a valid GEMINI_API_KEY."
        )

    headless = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() == "true"
    print(f"Launching Playwright browser (headless={headless})...")

    with sync_playwright() as p:
        # Load cookies first to extract timezone and locale
        cookies_to_add = req.cookies if req.cookies else []
        if not cookies_to_add:
            cookies_file_path = os.path.join(os.path.dirname(__file__), "..", "data", "cookies.json")
            if os.path.exists(cookies_file_path):
                try:
                    import json
                    with open(cookies_file_path, "r", encoding="utf-8") as f:
                        cookies_to_add = json.load(f)
                    print(f"Loaded {len(cookies_to_add)} cookies from local storage.")
                except Exception as cookie_err:
                    print(f"Failed to read cookies.json: {cookie_err}")
        
        timezone, locale = extract_timezone_and_locale(cookies_to_add)
        print(f"Playwright context in /apply configured with extracted locale={locale}, timezone={timezone}")
        
        browser, is_persistent = launch_browser(p, headless, locale=locale, timezone=timezone)
        if is_persistent:
            context = browser
            page = context.pages[0] if context.pages else context.new_page()
            if is_linkedin_url(req.url):
                login_ok = ensure_linkedin_logged_in(page)
                if not login_ok:
                    raise HTTPException(status_code=401, detail="LinkedIn authentication failed")
        else:
            user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=user_agent,
                locale=locale,
                timezone_id=timezone
            )
            
            if cookies_to_add:
                try:
                    sanitized = sanitize_cookies(cookies_to_add)
                    context.add_cookies(sanitized)
                    print(f"Successfully injected {len(sanitized)} sanitized session cookies into Playwright context.")
                except Exception as e:
                    print(f"Graceful warning: Failed to inject cookies: {e}")
            
            page = context.new_page()

        try:
            print(f"Navigating to: {req.url}")
            page.goto(req.url)
            page.wait_for_timeout(2000)  # Wait for load and transitions

            screenshot_dir = os.path.join(os.path.dirname(__file__), "..", "screenshots")
            os.makedirs(screenshot_dir, exist_ok=True)
            allow_structured_submit = "localhost" in req.url or "127.0.0.1" in req.url
            if try_structured_form_fill(page, req.resume_path, req.user_data, allow_submit=allow_structured_submit):
                final_screenshot_path = os.path.join(screenshot_dir, "final_confirmation.png")
                page.screenshot(path=final_screenshot_path)
                return {
                    "success": True,
                    "status": "Submitted",
                    "screenshot_path": final_screenshot_path,
                    "method": "structured_form_fill"
                }

            loop_limit = 12
            step = 0
            success = False
            error_message = None

            while step < loop_limit:
                step += 1
                print(f"--- Visual Automation Step {step} ---")
                
                # Dismiss popup modals before taking screenshot
                dismiss_popups(page)
                
                # 1. Take screenshot
                screenshot_bytes = page.screenshot(type="png")
                img_w, img_h = get_png_dimensions(screenshot_bytes)
                
                # Keep a local copy for debugging
                screenshot_filename = f"step_{step}_{int(time.time())}.png"
                local_screenshot_path = os.path.join(screenshot_dir, screenshot_filename)
                with open(local_screenshot_path, "wb") as f:
                    f.write(screenshot_bytes)
                
                # Check if page indicates submission success
                page_text = page.locator("body").inner_text()
                if "Application Submitted Successfully" in page_text or "Submitted" in page_text:
                    print("Success string detected on page!")
                    success = True
                    break

                # 2. Build Part with image
                image_part = types.Part.from_bytes(
                    data=screenshot_bytes,
                    mime_type="image/png"
                )

                 # 3. Call Gemini Vision
                prompt = f"""
You are the visual coordinator of a highly intelligent, visual web-scraping and browser-automation agent.
You are given a viewport screenshot of a job application page.

The screenshot has a physical resolution of {img_w}x{img_h} pixels.
You MUST return coordinate values ("x" and "y") relative to this {img_w}x{img_h} physical image coordinate space.

The user profile data you must fill into the form is:
{json.dumps(req.user_data, indent=2)}

Note: The user profile data might contain a "resume_text" field which represents the full text of their customized resume. If the form asks custom experience questions, refer to the "resume_text" to answer them!

The PDF resume file to upload is located at: "{req.resume_path}"

Your task is to analyze the screenshot, determine the next optimal action to fill out the form completely, and proceed to submission.
Instructions:
1. Match input fields (First Name, Last Name, Email, Phone, Resume, etc.) with corresponding coordinates.
2. If the page has custom questions (e.g. desired salary, sponsorship, links to LinkedIn/GitHub, gender, race, veteran status, cover letter, or experience questions), read the user profile and resume text to answer them intelligently.
   - For dropdowns: first click the dropdown element (action: "click"), then select the option in the next step.
   - For checkboxes or radio buttons: click the checkbox/radio option or its text label (action: "click").
   - If a checkbox is already checked, do NOT click it again.
3. If the page has multi-step forms (e.g. "Next", "Continue", "Save & Continue"), find the next button and click it (action: "click").
4. If you get stuck on an element or it keeps failing, try to skip it or scroll down.
5. If the form is completely filled and the resume is uploaded, find the submit button and submit the application (action: "click_submit").

Your output MUST be a valid JSON object matching this schema exactly (NO markdown code blocks, just raw JSON):
{{
  "status": "continue" or "completed" or "error",
  "element_name": "name of field/button being targeted",
  "x": integer, (pixel X coordinate)
  "y": integer, (pixel Y coordinate)
  "action": "click" or "click_type" or "click_upload" or "click_submit" or "scroll" or "wait",
  "text_to_type": "string" (only for click_type),
  "file_to_upload": "string" (only for click_upload),
  "scroll_direction": "down" or "up" (only for scroll)
}}
"""
                try:
                    response = client.models.generate_content(
                        model="gemini-3.5-flash",
                        contents=[image_part, prompt],
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            temperature=0.0
                        )
                    )
                    
                    action_text = response.text.strip()
                    if action_text.startswith("```json"):
                        action_text = action_text.replace("```json", "").replace("```", "").strip()
                    elif action_text.startswith("```"):
                        action_text = action_text.replace("```", "").strip()
                    
                    action_json = json.loads(action_text)
                    print(f"Gemini Action: {json.dumps(action_json, indent=2)}")
                except Exception as e:
                    print(f"Gemini API or JSON decode error: {e}")
                    error_message = f"Vision agent query failed: {e}"
                    break
 
                if action_json.get("status") == "completed":
                    success = True
                    break
                elif action_json.get("status") == "error":
                    error_message = f"Agent failed: {action_json.get('element_name')}"
                    break
 
                action = action_json.get("action")
                raw_x = action_json.get("x")
                raw_y = action_json.get("y")
                
                # Scale coordinates to CSS pixels
                x, y = scale_coordinates(raw_x, raw_y, img_w, img_h, page)
 
                if action == "click":
                    print(f"Action: Click at CSS ({x}, {y}) [raw: {raw_x}, {raw_y}]")
                    page.mouse.click(x, y)
                elif action == "click_type":
                    text = action_json.get("text_to_type", "")
                    print(f"Action: Click and Type '{text}' at CSS ({x}, {y}) [raw: {raw_x}, {raw_y}]")
                    page.mouse.click(x, y)
                    page.keyboard.type(text)
                
                elif action == "click_upload":
                    filepath = action_json.get("file_to_upload")
                    if not filepath:
                        filepath = req.resume_path
                    print(f"Action: Upload file '{filepath}' at CSS ({x}, {y}) [raw: {raw_x}, {raw_y}]")
                    
                    if not os.path.exists(filepath):
                        # Create a mock pdf resume if path doesn't exist for test
                        with open(filepath, "w") as f:
                            f.write("%PDF-1.4 Mock Resume")
                            
                    try:
                        with page.expect_file_chooser() as fc_info:
                            page.mouse.click(x, y)
                        file_chooser = fc_info.value
                        file_chooser.set_files(filepath)
                    except Exception as upload_err:
                        print(f"Expect file chooser coordinate click failed: {upload_err}. Trying standard upload selector fallback.")
                        # Fallback: check if standard file input is available
                        try:
                            page.set_input_files("input[type=file]", filepath)
                        except Exception as fb_err:
                            print(f"Fallback upload also failed: {fb_err}")
                
                elif action == "click_submit":
                    print(f"Action: Click Submit Button at CSS ({x}, {y}) [raw: {raw_x}, {raw_y}]")
                    page.mouse.click(x, y)
                    page.wait_for_timeout(3000)  # Wait for submission network requests
                
                elif action == "scroll":
                    direction = action_json.get("scroll_direction", "down")
                    scroll_y = 400 if direction == "down" else -400
                    print(f"Action: Scroll {direction}")
                    page.evaluate(f"window.scrollBy(0, {scroll_y})")
                
                elif action == "wait":
                    print("Action: Wait 2 seconds")
                    page.wait_for_timeout(2000)

                # Wait between actions for rendering
                page.wait_for_timeout(1500)

            # Final check and screenshot
            final_screenshot_path = os.path.join(screenshot_dir, "final_confirmation.png")
            page.screenshot(path=final_screenshot_path)
            
            if success:
                return {
                    "success": True,
                    "status": "Submitted",
                    "screenshot_path": final_screenshot_path
                }
            else:
                return {
                    "success": False,
                    "status": "Failed",
                    "error": error_message or "Reached visual loop limit without confirmation",
                    "screenshot_path": final_screenshot_path
                }

        except Exception as e:
            print(f"Playwright execution error: {e}")
            raise HTTPException(status_code=500, detail=f"Playwright loop error: {e}")
        finally:
            browser.close()

class SearchLinkedInRequest(BaseModel):
    keywords: List[str]
    location: str = "United States"
    date_posted: Optional[str] = None
    experience_level: Optional[List[str]] = None
    remote: Optional[List[str]] = None

@app.post("/search-linkedin")
def search_linkedin(req: SearchLinkedInRequest):
    if not client:
        raise HTTPException(
            status_code=500,
            detail="Gemini API Key is not configured. Visual search requires GEMINI_API_KEY."
        )

    headless = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() == "true"
    query = " ".join(req.keywords)
    
    # Construct search URL with filters
    base_url = "https://www.linkedin.com/jobs/search"
    params = [
        f"keywords={query.replace(' ', '%20')}",
        f"location={req.location.replace(' ', '%20')}"
    ]
    
    if req.date_posted:
        if req.date_posted == "past-24h":
            params.append("f_TPR=r86400")
        elif req.date_posted == "past-week":
            params.append("f_TPR=r604800")
        elif req.date_posted == "past-month":
            params.append("f_TPR=r2592000")
            
    if req.experience_level:
        exp_mapping = {
            "internship": "1",
            "entry-level": "2",
            "associate": "3",
            "mid-senior": "4",
            "director": "5",
            "executive": "6"
        }
        exp_vals = [exp_mapping[e] for e in req.experience_level if e in exp_mapping]
        if exp_vals:
            params.append(f"f_E={','.join(exp_vals)}")
            
    if req.remote:
        remote_mapping = {
            "on-site": "1",
            "remote": "2",
            "hybrid": "3"
        }
        remote_vals = [remote_mapping[r] for r in req.remote if r in remote_mapping]
        if remote_vals:
            params.append(f"f_WT={','.join(remote_vals)}")
            
    url = f"{base_url}?{'&'.join(params)}"
    
    print(f"Launching Playwright visual search for keywords={req.keywords} on URL: {url}...")
    
    with sync_playwright() as p:
        # Load cookies first to extract timezone and locale
        cookies_to_add = []
        cookies_file_path = os.path.join(os.path.dirname(__file__), "..", "data", "cookies.json")
        if os.path.exists(cookies_file_path):
            try:
                with open(cookies_file_path, "r", encoding="utf-8") as f:
                    cookies_to_add = json.load(f)
                print(f"Loaded {len(cookies_to_add)} cookies for search.")
            except Exception as cookie_err:
                print(f"Failed to read cookies.json for search: {cookie_err}")
        
        timezone, locale = extract_timezone_and_locale(cookies_to_add)
        print(f"Playwright context in /search-linkedin configured with extracted locale={locale}, timezone={timezone}")
        
        browser, is_persistent = launch_browser(p, headless, locale=locale, timezone=timezone)
        if is_persistent:
            context = browser
            page = context.pages[0] if context.pages else context.new_page()
            login_ok = ensure_linkedin_logged_in(page)
            if not login_ok:
                raise HTTPException(status_code=401, detail="LinkedIn authentication failed")
        else:
            user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=user_agent,
                locale=locale,
                timezone_id=timezone
            )
            
            if cookies_to_add:
                try:
                    sanitized = sanitize_cookies(cookies_to_add)
                    context.add_cookies(sanitized)
                    print(f"Successfully injected {len(sanitized)} sanitized session cookies into search context.")
                except Exception as e:
                    print(f"Failed to inject cookies for search: {e}")
                    
            page = context.new_page()
        
        # Define context-level listener state for late-opening tabs
        active_expecting_page = False
        latest_captured_page = None

        def handle_page_opened(new_p):
            nonlocal active_expecting_page, latest_captured_page
            if active_expecting_page:
                latest_captured_page = new_p
                print(f"[Playwright Listener] Expected new page opened: {new_p.url}")
            else:
                print(f"[Playwright Listener] Closing unexpected late page immediately.")
                try:
                    new_p.close()
                except Exception as close_err:
                    print(f"[Playwright Listener Warning] Failed to close unexpected page: {close_err}")

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector(
                    ".jobs-search-results-list, .scaffold-layout__list, [data-job-id], .job-card-container",
                    timeout=15000,
                )
            except Exception as list_wait_err:
                print(f"[LinkedIn Search] Job list selector wait timed out; continuing with screenshot fallback: {list_wait_err}")
            page.wait_for_timeout(4000) # Wait for page and jobs sidebar to load
            dismiss_popups(page)
            
            # Take screenshot of list
            screenshot_bytes = page.screenshot(type="png")
            img_w, img_h = get_png_dimensions(screenshot_bytes)
            try:
                list_screenshot_path = os.path.join(os.path.dirname(__file__), "..", "data", "linkedin_search_list.png")
                os.makedirs(os.path.dirname(list_screenshot_path), exist_ok=True)
                with open(list_screenshot_path, "wb") as f:
                    f.write(screenshot_bytes)
                print(f"Saved search list screenshot to {list_screenshot_path}")
            except Exception as e:
                print(f"Failed to save search list screenshot: {e}")

            dom_results = extract_linkedin_jobs_dom(page, context, url, max_jobs=3)
            if dom_results:
                print(f"[LinkedIn DOM] Returning {len(dom_results)} jobs with extracted external apply links.")
                return {"jobs": dom_results}
            print("[LinkedIn DOM] No complete DOM results found. Falling back to visual extraction.")

            context.on("page", handle_page_opened)
            image_part = types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png")
            
            prompt_list = f"""
This is a LinkedIn job search page screenshot. We want to extract the first 3 job postings listed on the left panel (the job list).

The screenshot has a physical resolution of {img_w}x{img_h} pixels.
You MUST return coordinate values ("x" and "y") relative to this {img_w}x{img_h} physical image coordinate system.

For each job card, extract:
1. The Job Title (e.g. 'Software Engineer')
2. The Company Name
3. The Location
4. The exact center coordinate (x, y) of the job card in the left list so we can click on it.

Return ONLY a valid JSON list matching this schema (NO markdown code blocks):
[
  {{
    "title": "Job Title",
    "company": "Company Name",
    "location": "Location",
    "x": integer,
    "y": integer
  }}
]
"""
            response = client.models.generate_content(
                model="gemini-3.5-flash",
                contents=[image_part, prompt_list],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.0
                )
            )
            
            list_text = response.text.strip()
            if list_text.startswith("```json"):
                list_text = list_text.replace("```json", "").replace("```", "").strip()
            elif list_text.startswith("```"):
                list_text = list_text.replace("```", "").strip()
                
            jobs_list = json.loads(list_text)
            print(f"Detected {len(jobs_list)} jobs from vision search page.")
            
            results = []
            for idx, job in enumerate(jobs_list[:3]): # Max 3 jobs for speed
                # Close any extra pages/tabs to ensure a clean state before processing this job
                while len(context.pages) > 1:
                    try:
                        extra_page = context.pages[-1]
                        if extra_page != page:
                            print(f"[Playwright] Closing leftover tab: {extra_page.url}")
                            extra_page.close()
                        else:
                            first_page = context.pages[0]
                            if first_page != page:
                                print(f"[Playwright] Closing leftover first tab: {first_page.url}")
                                first_page.close()
                            else:
                                break
                    except Exception as close_err:
                        print(f"[Playwright Warning] Failed to close leftover page: {close_err}")
                        break

                raw_x = job.get("x")
                raw_y = job.get("y")
                title = job.get("title")
                company = job.get("company")
                location = job.get("location")
                
                # Scale coordinates to CSS pixels
                x, y = scale_coordinates(raw_x, raw_y, img_w, img_h, page)
                
                print(f"Clicking job card {idx+1}: {title} at {company} CSS ({x}, {y}) [raw: {raw_x}, {raw_y}]")
                page.mouse.click(x, y)
                page.wait_for_timeout(2500) # Wait for right pane to load
                
                # Dismiss popups that might appear after clicking card
                dismiss_popups(page)
                
                # Initialize fallback variable; we will extract it lazily later if external redirection fails
                fallback_linkedin_jd = ""

                # --- LLM-DRIVEN JOB DETAILS AGENT LOOP (APPLY FIRST) ---
                history = []
                final_jd = ""
                apply_link = page.url
                
                details_step = 0
                max_details_steps = 8
                skip_job = False
                clicked_apply = False
                apply_x, apply_y = None, None
                
                while details_step < max_details_steps:
                    details_step += 1
                    print(f"--- Job details agent step {details_step} ---")
                    
                    # Dismiss popups
                    dismiss_popups(page)
                    
                    # Take screenshot
                    detail_screenshot = page.screenshot(type="png")
                    detail_w, detail_h = get_png_dimensions(detail_screenshot)
                    
                    # Save local copy for debugging
                    try:
                        detail_screenshot_path = os.path.join(os.path.dirname(__file__), "..", "data", f"linkedin_job_detail_{idx}_{details_step}.png")
                        os.makedirs(os.path.dirname(detail_screenshot_path), exist_ok=True)
                        with open(detail_screenshot_path, "wb") as f:
                            f.write(detail_screenshot)
                    except Exception as e:
                        print(f"Failed to save detail screenshot: {e}")
                        
                    detail_part = types.Part.from_bytes(data=detail_screenshot, mime_type="image/png")
                    
                    prompt_detail = f"""
You are the visual coordinator of a browser automation agent.
You are looking at the active LinkedIn job details page.
Our goal is to click the external "Apply" button to open the external job application page.
Do NOT click "Easy Apply" (简单申请/Easy Apply). If it is "Easy Apply", we must skip this job.

Previous actions taken in this loop:
{json.dumps(history, indent=2)}

Analyze the screenshot and page state. Choose the next action:
- "click_apply": If you see the external "Apply" button (usually says "Apply" or "申请", NOT "Easy Apply" or "简单申请"), click it. Provide its center coordinates ("x", "y").
- "skip": If the job is "Easy Apply" (简单申请), or if we already applied, or if this job is not suitable.
- "click": If there is a popup, overlay, or cookie consent blocking the view, click to dismiss it. Provide the center coordinates ("x", "y").
- "wait": If the page is still loading or rendering.
- "error": If we are stuck or cannot proceed.

Note: The screenshot has a physical resolution of {detail_w}x{detail_h} pixels.
Your coordinates must be in this coordinate space.

Return ONLY a valid JSON object matching this schema (do NOT wrap it in markdown, just return the raw JSON):
{{
  "action": "click_apply" | "skip" | "click" | "wait" | "error",
  "x": integer or null,
  "y": integer or null,
  "reason": "explanation of what you see and why you chose this action"
}}
"""
                    try:
                        detail_response = client.models.generate_content(
                            model="gemini-3.5-flash",
                            contents=[detail_part, prompt_detail],
                            config=types.GenerateContentConfig(
                                response_mime_type="application/json",
                                temperature=0.0
                            )
                        )
                        
                        detail_text = detail_response.text.strip()
                        if detail_text.startswith("```json"):
                            detail_text = detail_text.replace("```json", "").replace("```", "").strip()
                        elif detail_text.startswith("```"):
                            detail_text = detail_text.replace("```", "").strip()
                            
                        detail_json = json.loads(detail_text)
                        action = detail_json.get("action")
                        reason = detail_json.get("reason", "")
                        print(f"[Playwright LLM Details] Step {details_step} | Action: {action} | Reason: {reason}")
                    except Exception as e:
                        print(f"Gemini API or JSON decode error in details loop: {e}")
                        history.append(f"Error at step {details_step}: {e}")
                        page.wait_for_timeout(2000)
                        continue
                        
                    if action == "skip":
                        print(f"Skipping job: '{title}' at '{company}' as requested by LLM.")
                        skip_job = True
                        break
                    elif action == "error":
                        print(f"Error reported by LLM in details loop: {reason}")
                        break
                    elif action == "wait":
                        print("Waiting 2 seconds...")
                        page.wait_for_timeout(2000)
                        history.append("Waited 2 seconds for page load")
                    elif action == "click":
                        raw_click_x = detail_json.get("x")
                        raw_click_y = detail_json.get("y")
                        if raw_click_x and raw_click_y:
                            cx, cy = scale_coordinates(raw_click_x, raw_click_y, detail_w, detail_h, page)
                            print(f"[Playwright LLM Details] Clicking at CSS ({cx}, {cy}) [raw: {raw_click_x}, {raw_click_y}]...")
                            page.mouse.click(cx, cy)
                            page.wait_for_timeout(1500)
                            history.append(f"Clicked coordinates ({raw_click_x}, {raw_click_y}) to expand/dismiss")
                        else:
                            print("[Playwright LLM Details Warning] Click requested but coordinates missing.")
                            page.wait_for_timeout(1500)
                    elif action == "click_apply":
                        raw_apply_x = detail_json.get("x")
                        raw_apply_y = detail_json.get("y")
                        if raw_apply_x and raw_apply_y:
                            apply_x, apply_y = scale_coordinates(raw_apply_x, raw_apply_y, detail_w, detail_h, page)
                            print(f"[Playwright LLM Details] Clicking Apply button at CSS ({apply_x}, {apply_y}) [raw: {raw_apply_x}, {raw_apply_y}]...")
                            
                            # Enable expecting page flag to allow the new tab to open and persist
                            active_expecting_page = True
                            latest_captured_page = None
                            
                            # Try to click using dispatch_event on selector first to bypass Ember event delegation issues.
                            # Fallback to mouse click if selector is not found or fails.
                            clicked_btn = False
                            try:
                                apply_btn = page.locator(".jobs-apply-button:visible, button[class*='apply']:visible, a[class*='apply']:visible").first
                                if apply_btn.count() > 0:
                                    apply_btn.dispatch_event("click")
                                    print("[Playwright] Dispatched click event to Apply button successfully.")
                                    clicked_btn = True
                            except Exception as click_err:
                                print(f"[Playwright Warning] Dispatched click event to Apply button failed: {click_err}")
                                
                            if not clicked_btn:
                                print("[Playwright] Falling back to mouse coordinates click on Apply button.")
                                page.mouse.click(apply_x, apply_y)
                                
                            page.wait_for_timeout(3000)
                            clicked_apply = True
                            history.append("Clicked external Apply button")
                            break # Exit details loop to process redirection
                        else:
                            print("[Playwright LLM Details Warning] Click Apply requested but coordinates missing.")
                            page.wait_for_timeout(1500)
                            
                if skip_job:
                    continue
                    
                if clicked_apply:
                    print(f"Processing external redirection page/tab...")
                    try:
                        # 2. Find the target tab/page
                        # If a new page opened, switch to it. Otherwise stay on the current page.
                        target_page = page
                        if latest_captured_page:
                            target_page = latest_captured_page
                        elif len(context.pages) > 1:
                            for p in context.pages:
                                if p != page and not is_linkedin_jobs_search_url(p.url) and p.url != "about:blank":
                                    target_page = p
                                    break
                            if target_page == page:
                                target_page = context.pages[-1]
                        
                        print(f"[Playwright] Target page for external apply redirect identified: {target_page.url}")
                        
                        # 3. Enter LLM-driven visual redirection loop
                        redirect_success = False
                        redirect_step = 0
                        max_redirect_steps = 5
                        
                        while redirect_step < max_redirect_steps:
                            redirect_step += 1
                            
                            # Dynamically determine the target_page at the start of each redirect step.
                            # This handles cases where a click in the previous step opened a new tab.
                            if latest_captured_page:
                                target_page = latest_captured_page
                            elif len(context.pages) > 1:
                                for p in context.pages:
                                    if p != page and not is_linkedin_jobs_search_url(p.url) and p.url != "about:blank":
                                        target_page = p
                                        break
                                if target_page == page:
                                    target_page = context.pages[-1]
                            else:
                                target_page = page
                                
                            print(f"--- Redirect Visual Step {redirect_step} on {target_page.url} ---")
                            
                            # We do not dismiss popups automatically here to avoid closing warning modals like 'You are leaving LinkedIn'.
                            # The visual LLM agent will handle any popups or warnings.
                            
                            # Take screenshot
                            r_screenshot = target_page.screenshot(type="png")
                            r_w, r_h = get_png_dimensions(r_screenshot)
                            r_part = types.Part.from_bytes(data=r_screenshot, mime_type="image/png")
                            
                            # Save screenshot locally for review
                            try:
                                redirect_screenshot_path = os.path.join(os.path.dirname(__file__), "..", "data", f"linkedin_job_redirect_{idx}_{redirect_step}.png")
                                os.makedirs(os.path.dirname(redirect_screenshot_path), exist_ok=True)
                                with open(redirect_screenshot_path, "wb") as f:
                                    f.write(r_screenshot)
                                print(f"Saved job {idx} redirect step {redirect_step} screenshot to {redirect_screenshot_path}")
                            except Exception as e:
                                print(f"Failed to save job {idx} redirect step {redirect_step} screenshot: {e}")
                            
                            prompt_redirect = f"""
Analyze this browser page screenshot after clicking the job's "Apply" button.
We want to reach the final, external company job application page (e.g. Workday, Greenhouse, Lever, company website, etc.).

Current physical screenshot dimensions: {r_w}x{r_h} pixels.
You MUST return coordinate values relative to this {r_w}x{r_h} coordinate space.

Current state tasks:
1. Is this the final company application page where the candidate can see the job description or fill out the application form? If YES, set "action" to "success".
2. Is this a blank page, loading spinner, or transition screen that is still loading? If YES, set "action" to "wait".
3. Is this a warning popup (like "You are leaving LinkedIn"), a verification step, or a cookie agreement that requires clicking a button to continue? (e.g., "Continue", "Continue applying"). If YES, set "action" to "click", and return the center coordinates of that button in "x" and "y".
4. Is it showing an error, block screen, or something we cannot bypass? If YES, set "action" to "error".

Return ONLY a valid JSON object matching this schema:
{{
  "action": "success" or "wait" or "click" or "error",
  "x": integer or null,
  "y": integer or null,
  "reason": "explanation of what you see"
}}
"""
                            r_resp = client.models.generate_content(
                                model="gemini-3.5-flash",
                                contents=[r_part, prompt_redirect],
                                config=types.GenerateContentConfig(
                                    response_mime_type="application/json",
                                    temperature=0.0
                                )
                            )
                            
                            r_text = r_resp.text.strip()
                            if r_text.startswith("```json"):
                                r_text = r_text.replace("```json", "").replace("```", "").strip()
                            elif r_text.startswith("```"):
                                r_text = r_text.replace("```", "").strip()
                                
                            r_json = json.loads(r_text)
                            action = r_json.get("action")
                            reason = r_json.get("reason", "")
                            print(f"[Playwright LLM Redirect] Action: {action} | Reason: {reason}")
                            
                            if action == "success":
                                apply_link = target_page.url
                                print(f"[Playwright] Successfully reached final application link: {apply_link}")
                                redirect_success = True
                                break
                            elif action == "wait":
                                print("[Playwright] LLM requested to wait for redirect/load...")
                                target_page.wait_for_timeout(2500)
                                # Save screenshot post-wait
                                try:
                                    post_wait_screenshot = target_page.screenshot(type="png")
                                    post_wait_path = os.path.join(os.path.dirname(__file__), "..", "data", f"linkedin_job_redirect_{idx}_{redirect_step}_after_wait.png")
                                    with open(post_wait_path, "wb") as f:
                                        f.write(post_wait_screenshot)
                                    print(f"Saved job {idx} redirect step {redirect_step} post-wait screenshot to {post_wait_path}")
                                except Exception as wait_ss_err:
                                    print(f"Failed to save post-wait screenshot: {wait_ss_err}")
                            elif action == "click":
                                rx = r_json.get("x")
                                ry = r_json.get("y")
                                if rx and ry:
                                    cx, cy = scale_coordinates(rx, ry, r_w, r_h, target_page)
                                    print(f"[Playwright] LLM requested click at CSS ({cx}, {cy}) [raw: {rx}, {ry}]...")
                                    target_page.mouse.click(cx, cy)
                                    target_page.wait_for_timeout(2500)
                                    # Save screenshot post-click
                                    try:
                                        post_click_screenshot = target_page.screenshot(type="png")
                                        post_click_path = os.path.join(os.path.dirname(__file__), "..", "data", f"linkedin_job_redirect_{idx}_{redirect_step}_after_click.png")
                                        with open(post_click_path, "wb") as f:
                                            f.write(post_click_screenshot)
                                        print(f"Saved job {idx} redirect step {redirect_step} post-click screenshot to {post_click_path}")
                                    except Exception as click_ss_err:
                                        print(f"Failed to save post-click screenshot: {click_ss_err}")
                                else:
                                    print("[Playwright Warning] LLM requested click but coordinates x/y were missing.")
                                    target_page.wait_for_timeout(2000)
                            elif action == "error":
                                print(f"[Playwright] LLM reported error state on redirect: {reason}")
                                break
                            else:
                                print(f"[Playwright Warning] Unknown action returned: {action}")
                                target_page.wait_for_timeout(2000)
                        
                        if not redirect_success:
                            # Fallback if loop ended without success status
                            apply_link = target_page.url
                            print(f"[Playwright Warning] Redirect loop completed without explicit LLM success. Using current URL: {apply_link}")
                        
                        # 4. Extract external page JD before closing the tab
                        ext_body_text = ""
                        try:
                            ext_body_text = target_page.locator("body").inner_text()
                        except Exception as text_err:
                            print(f"[Playwright Warning] Failed to get external body text: {text_err}")
                            
                        ext_screenshot = target_page.screenshot(type="png")
                        try:
                            ext_screenshot_path = os.path.join(os.path.dirname(__file__), "..", "data", f"linkedin_job_external_{idx}.png")
                            os.makedirs(os.path.dirname(ext_screenshot_path), exist_ok=True)
                            with open(ext_screenshot_path, "wb") as f:
                                f.write(ext_screenshot)
                            print(f"Saved job {idx} external page screenshot to {ext_screenshot_path}")
                        except Exception as e:
                            print(f"Failed to save job {idx} external page screenshot: {e}")
                            
                        # If target_page is a new tab, close it.
                        # If it is the original search page (redirected in-place), restore it!
                        if target_page != page:
                            target_page.close()
                        else:
                            # Restoring original page from in-place redirect
                            try:
                                print("[Playwright] Restoring search page from in-place redirect...")
                                page.go_back()
                                page.wait_for_timeout(3000)
                            except Exception as go_back_err:
                                print(f"[Playwright Warning] Failed to go back: {go_back_err}. Reloading search URL.")
                                try:
                                    page.goto(url)
                                    page.wait_for_timeout(4000)
                                except Exception as goto_err:
                                    print(f"[Playwright Error] Failed to restore search URL: {goto_err}")
                                    
                        # Process screenshot & body text using Gemini to get full JD
                        ext_part = types.Part.from_bytes(data=ext_screenshot, mime_type="image/png")
                        
                        prompt_ext = f"""
Analyze this external job application page. Extract the complete, detailed Job Description (JD) / Job Requirements text.
Do not summarize or truncate.

Webpage text content:
--- WEBPAGE TEXT CONTENT START ---
{ext_body_text}
--- WEBPAGE TEXT CONTENT END ---

Return ONLY a valid JSON object matching this schema:
{{
  "job_description": "extracted full text job requirements..."
}}
"""
                        ext_resp = client.models.generate_content(
                            model="gemini-3.5-flash",
                            contents=[ext_part, prompt_ext],
                            config=types.GenerateContentConfig(
                                response_mime_type="application/json",
                                temperature=0.0
                            )
                        )
                        
                        ext_text = ext_resp.text.strip()
                        if ext_text.startswith("```json"):
                            ext_text = ext_text.replace("```json", "").replace("```", "").strip()
                        elif ext_text.startswith("```"):
                            ext_text = ext_text.replace("```", "").strip()
                            
                        ext_json = json.loads(ext_text)
                        extracted_jd = ext_json.get("job_description", "")
                        if extracted_jd.strip():
                            final_jd = extracted_jd.strip()
                            print("Successfully extracted detailed JD from the external page.")
                    except Exception as ext_err:
                        print(f"External apply link navigation skipped/failed: {ext_err}. Using LinkedIn page details instead.")
                    finally:
                        active_expecting_page = False
                    
                final_jd = final_jd.strip()
                if not final_jd or len(final_jd) < 200:
                    print(f"[Playwright] External JD is empty or too short ({len(final_jd)} chars). Falling back to LinkedIn details...")
                    lazy_fallback_jd = "No fallback description extracted"
                    try:
                        body_text_fallback = page.locator("body").inner_text()
                        prompt_fallback = f"""
We need a fallback Job Description (JD) / Job Requirements from this LinkedIn page text.
Please extract all details including responsibilities, requirements, and qualifications.
Do not summarize, extract exactly as written.

Webpage text content:
{body_text_fallback}

Return ONLY a valid JSON object matching this schema:
{{
  "job_description": "the complete extracted job description text..."
}}
"""
                        fallback_resp = client.models.generate_content(
                            model="gemini-3.5-flash",
                            contents=prompt_fallback,
                            config=types.GenerateContentConfig(
                                response_mime_type="application/json",
                                temperature=0.0
                            )
                        )
                        fallback_json = json.loads(fallback_resp.text.strip())
                        lazy_fallback_jd = fallback_json.get("job_description", "").strip()
                        print(f"[Playwright] Successfully extracted lazy fallback LinkedIn JD (length: {len(lazy_fallback_jd)} characters).")
                    except Exception as fallback_err:
                        print(f"[Playwright Warning] Failed to extract fallback LinkedIn JD: {fallback_err}")
                    final_jd = lazy_fallback_jd

                if not apply_link or is_linkedin_url(apply_link):
                    print(f"[Playwright] Skipping job without external apply link: {title} @ {company} ({apply_link})")
                    continue
                if not final_jd or len(final_jd.strip()) < 120:
                    print(f"[Playwright] Skipping job without a usable JD: {title} @ {company}")
                    continue
                
                # Save extracted JD to a local file for user inspection
                try:
                    jd_dir = os.path.join(os.path.dirname(__file__), "..", "data", "extracted_jds")
                    os.makedirs(jd_dir, exist_ok=True)
                    clean_company = "".join(c for c in company if c.isalnum() or c in (" ", "-", "_")).strip()
                    clean_role = "".join(c for c in title if c.isalnum() or c in (" ", "-", "_")).strip()
                    filename = f"jd_{idx+1}_{clean_company}_{clean_role}.txt".replace(" ", "_")
                    jd_file_path = os.path.join(jd_dir, filename)
                    
                    with open(jd_file_path, "w", encoding="utf-8") as f:
                        f.write(f"Company: {company}\n")
                        f.write(f"Role: {title}\n")
                        f.write(f"Location: {location}\n")
                        f.write(f"Apply Link: {apply_link}\n")
                        f.write("="*40 + "\n")
                        f.write(final_jd)
                    print(f"[Playwright] Saved final extracted JD to local file for user inspection: {jd_file_path}")
                except Exception as save_err:
                    print(f"[Playwright Warning] Failed to save JD to local file: {save_err}")
                    
                results.append({
                    "job_id": f"linkedin-{idx}-{int(time.time())}",
                    "company": company,
                    "role": title,
                    "job_description": final_jd,
                    "location": location,
                    "apply_link": apply_link
                })
                
            return {"jobs": results}
            
        except Exception as e:
            print(f"LinkedIn visual search failed: {e}")
            raise HTTPException(status_code=500, detail=f"Visual search failed: {e}")
        finally:
            browser.close()

if __name__ == "__main__":
    port = int(os.getenv("PORT") or os.getenv("PLAYWRIGHT_SERVER_PORT", 8004))
    uvicorn.run(app, host="0.0.0.0", port=port)
