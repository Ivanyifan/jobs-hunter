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

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from adapters.workday import (
        CountrySelectorHandler,
        EducationRepeatableSectionHandler,
        ExecutionState,
        ExperienceRepeatableSectionHandler,
    )
    WORKDAY_ADAPTER_IMPORT_ERROR = None
except Exception as _workday_adapter_error:
    CountrySelectorHandler = None
    EducationRepeatableSectionHandler = None
    ExecutionState = None
    ExperienceRepeatableSectionHandler = None
    WORKDAY_ADAPTER_IMPORT_ERROR = str(_workday_adapter_error)

try:
    from application_questions.detector import (
        canonical_key_for_question,
        clean_question_candidate,
        detect_visible_required_questions,
        detector_questions_payload,
        is_sensitive_question,
        outcome_status_for_questions,
    )
    from application_questions.fingerprint import fingerprint_question, normalize_question_text
    from application_questions.models import (
        BLOCKED_ON_QUESTIONS,
        DetectedQuestion,
        NEEDS_TECHNICAL_REVIEW,
        READY_TO_SUBMIT,
        TECHNICAL_REVIEW,
        UNANSWERED,
    )
    APPLICATION_QUESTIONS_IMPORT_ERROR = None
except Exception as _application_questions_error:
    canonical_key_for_question = lambda text, context=None: None
    clean_question_candidate = lambda text: str(text or "").strip()
    detect_visible_required_questions = None
    detector_questions_payload = None
    DetectedQuestion = None
    fingerprint_question = None
    normalize_question_text = None
    is_sensitive_question = lambda text: False
    outcome_status_for_questions = None
    BLOCKED_ON_QUESTIONS = "BLOCKED_ON_QUESTIONS"
    NEEDS_TECHNICAL_REVIEW = "NEEDS_TECHNICAL_REVIEW"
    READY_TO_SUBMIT = "READY_TO_SUBMIT"
    TECHNICAL_REVIEW = "TECHNICAL_REVIEW"
    UNANSWERED = "UNANSWERED"
    APPLICATION_QUESTIONS_IMPORT_ERROR = str(_application_questions_error)

# Configure stdout/stderr to use UTF-8 to prevent encoding errors on non-UTF-8 terminals
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

LIVE_PROGRESS_STATE = {}

def write_live_smoke_progress(page=None, stage=None, action=None, last_field=None, screenshot_path=None, dom_excerpt=None, **extra):
    progress_file = os.getenv("PLAYWRIGHT_PROGRESS_FILE")
    if not progress_file:
        return
    now = time.time()
    started_at = float(os.getenv("PLAYWRIGHT_PROGRESS_STARTED_AT") or LIVE_PROGRESS_STATE.get("started_at") or now)
    current_stage = stage or LIVE_PROGRESS_STATE.get("current_stage") or "unknown"
    if current_stage != LIVE_PROGRESS_STATE.get("current_stage"):
        LIVE_PROGRESS_STATE["stage_started_at"] = now
    stage_started_at = LIVE_PROGRESS_STATE.get("stage_started_at") or now
    current_url = ""
    if page is not None:
        try:
            current_url = page.url or ""
        except Exception:
            current_url = ""
    if not current_url:
        current_url = LIVE_PROGRESS_STATE.get("current_url") or ""
    screenshot_dir = os.getenv("PLAYWRIGHT_PROGRESS_SCREENSHOT_DIR")
    if page is not None and screenshot_dir and not screenshot_path:
        last_screenshot_at = float(LIVE_PROGRESS_STATE.get("last_progress_screenshot_at") or 0)
        if now - last_screenshot_at >= 25:
            try:
                os.makedirs(screenshot_dir, exist_ok=True)
                safe_run_id = re.sub(r"[^a-zA-Z0-9_.-]+", "_", os.getenv("PLAYWRIGHT_PROGRESS_RUN_ID") or "live_smoke")
                screenshot_path = os.path.join(screenshot_dir, f"{safe_run_id}_{int(now)}.png")
                page.screenshot(path=screenshot_path, full_page=True, timeout=5000)
                LIVE_PROGRESS_STATE["last_progress_screenshot_at"] = now
            except Exception:
                screenshot_path = LIVE_PROGRESS_STATE.get("screenshot_path") or ""
    if dom_excerpt is None and page is not None:
        try:
            dom_excerpt = compact_text(page_body_text(page, timeout=500))[:2000]
        except Exception:
            dom_excerpt = LIVE_PROGRESS_STATE.get("dom_excerpt") or ""
    payload = {
        "run_id": os.getenv("PLAYWRIGHT_PROGRESS_RUN_ID") or "",
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "updated_at_epoch": now,
        "started_at_epoch": started_at,
        "elapsed_seconds": round(now - started_at, 1),
        "current_stage": current_stage,
        "stage_started_at_epoch": stage_started_at,
        "stage_elapsed_seconds": round(now - stage_started_at, 1),
        "current_url": current_url,
        "last_action": action or LIVE_PROGRESS_STATE.get("last_action") or "",
        "last_field": last_field or LIVE_PROGRESS_STATE.get("last_field") or "",
        "screenshot_path": screenshot_path or LIVE_PROGRESS_STATE.get("screenshot_path") or "",
        "dom_excerpt": dom_excerpt or "",
    }
    payload.update({key: value for key, value in extra.items() if value is not None})
    LIVE_PROGRESS_STATE.update(payload)
    try:
        os.makedirs(os.path.dirname(progress_file), exist_ok=True)
        temp_path = f"{progress_file}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, progress_file)
    except Exception as err:
        print(f"[Progress] Failed to write live smoke progress: {err}")

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

WORKDAY_COUNTRY_SELECTION_DEBUG = []

def add_workday_country_debug(event, **kwargs):
    try:
        payload = {"event": event, "ts": int(time.time())}
        payload.update(kwargs)
        WORKDAY_COUNTRY_SELECTION_DEBUG.append(payload)
        del WORKDAY_COUNTRY_SELECTION_DEBUG[:-80]
    except Exception:
        pass

def email_service_url(path):
    base_url = (os.getenv("EMAIL_URL") or "").rstrip("/")
    if not base_url:
        email_server_port = int(os.getenv("EMAIL_SERVER_PORT", 8005))
        base_url = f"http://localhost:{email_server_port}"
    return f"{base_url}{path}"

def mongo_service_url(path):
    base_url = (os.getenv("MONGO_URL") or "").rstrip("/")
    if not base_url:
        mongo_server_port = int(os.getenv("MONGO_SERVER_PORT", 8001))
        base_url = f"http://localhost:{mongo_server_port}"
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
                viewport={"width": 1280, "height": 1100},
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
        if "cookie" in body_text:
            for pattern in [
                re.compile(r"^\s*accept\s+cookies\s*$", re.IGNORECASE),
                re.compile(r"^\s*accept\s+all\s*$", re.IGNORECASE),
                re.compile(r"^\s*accept\s*$", re.IGNORECASE),
            ]:
                try:
                    button = page.locator('button, [role="button"], a').filter(has_text=pattern)
                    if button.count() > 0 and button.first.is_visible():
                        print("[Playwright] Accepting cookie banner.")
                        button.first.click()
                        page.wait_for_timeout(1000)
                        break
                except Exception:
                    pass
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

BARE_FINAL_SUBMIT_RE = re.compile(r"^\s*(submit|send|complete|finish)\s*$", re.IGNORECASE)

SUBMISSION_SUCCESS_RE = re.compile(
    r"(thank you for (applying|submitting)|"
    r"application (has been |was |is )?(submitted|received|complete)|"
    r"successfully submitted|"
    r"we (have|'ve) received your application|"
    r"your application has been received)",
    re.IGNORECASE,
)

HIGH_RISK_FIELD_RE = re.compile(
    r"(sponsor|visa|work authorization|authorized to work|citizen|eeo|disability|veteran|gender|race|ethnicity|"
    r"criminal|background check|salary|compensation|relocat|date of birth|\bage\b)",
    re.IGNORECASE
)

MEDIUM_RISK_FIELD_RE = re.compile(
    r"(cover letter|why|experience|years|education|school|degree|address|resume|cv|upload|portfolio)",
    re.IGNORECASE
)

SAFE_DISCOVERY_CHECKBOX_RE = re.compile(
    r"\b(use my profile|build resume|resume builder|import profile|use existing profile|copy from profile|applicant privacy notice|privacy notice|terms and conditions)\b",
    re.IGNORECASE
)

PROBE_TEXT_PLACEHOLDER = "TO_BE_REVIEWED_BY_APPLICANT"
PROBE_SUPPORTED_CONTROL_TYPES = {"select", "radio", "checkbox", "text", "textarea"}
PROBE_IMMEDIATE_STOP_RE = re.compile(
    r"(captcha|bot challenge|electronic signature|signature|certif|attest|i certify|i confirm this is true|"
    r"under penalty|true and complete|accurate and complete|government or public institution|public body|"
    r"regulatory authority|non-compete|export control)",
    re.IGNORECASE,
)
PROBE_BRANCH_WARNING = "Later questions may depend on this temporary answer"
TRUSTED_QUESTION_ALIAS_PATTERNS = {
    "sponsorship": [r"\bsponsor(ship)?\b", r"\bvisa\b.*\bsponsor"],
    "visa_sponsorship": [r"\bsponsor(ship)?\b", r"\bvisa\b"],
    "requires_sponsorship": [r"\bsponsor(ship)?\b", r"\bvisa\b.*\bsponsor"],
    "need_sponsorship": [r"\brequire sponsorship\b", r"\bsponsor(ship)?\b", r"\bvisa\b.*\bsponsor"],
    "work_authorization": [r"\bauthorized?\b.*\bwork\b", r"\blegally\b.*\bwork\b", r"\bwork authorization\b"],
    "authorized_to_work": [r"\bauthorized?\b.*\bwork\b", r"\blegally\b.*\bwork\b", r"\bwork authorization\b"],
    "authorized_to_work_us": [r"\bauthorized?\b.*\bwork\b", r"\blegally\b.*\bwork\b", r"\bwork authorization\b"],
    "conflict_of_interest": [r"\bconflict of interest\b", r"\boutside employment\b.*\bcompetitor\b", r"\bsignificant financial interest\b"],
    "export_control": [r"\bexport control\b", r"\bcitizen\b.*\bpermanent resident\b", r"\bcitizenship/permanent residency\b"],
    "current_or_previous_company_employee": [r"\bexisting\b.*\bemployee\b", r"\bcurrent\b.*\bemployee\b", r"\bprevious\b.*\bemployee\b"],
    "salary_expectation": [r"\bsalary\b", r"\bcompensation\b", r"\bpay\b.*\bexpect"],
    "compensation_expectation": [r"\bsalary\b", r"\bcompensation\b", r"\bpay\b.*\bexpect"],
    "relocation": [r"\brelocat(e|ion|ing)?\b"],
    "start_date": [r"\bstart date\b", r"\bearliest\b.*\bstart\b", r"\bavailable\b.*\bstart\b", r"\bwhen\b.*\bstart\b", r"\bnotice period\b"],
    "notice_period": [r"\bnotice period\b", r"\bearliest\b.*\bstart\b"],
    "veteran_status": [r"\bveteran\b"],
    "disability_status": [r"\bdisabilit(y|ies)\b"],
}

DEFAULT_EDUCATION_PROFILE = {
    "school": "University of Illinois at Urbana-Champaign",
    "degree": "Bachelor of Science in Computer Science and Linguistics",
    "field": "Computer Science and Linguistics",
    "location": "Champaign, IL",
    "start_month": "August",
    "start_year": "2021",
    "end_month": "December",
    "end_year": "2026",
}

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

def extract_resume_text_from_path(resume_path):
    if not resume_path or not os.path.exists(resume_path):
        return ""
    ext = os.path.splitext(resume_path)[1].lower()
    try:
        if ext == ".pdf":
            import pypdf
            reader = pypdf.PdfReader(resume_path)
            return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
        if ext in {".txt", ".md"}:
            with open(resume_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read().strip()
    except Exception as err:
        print(f"[Resume Text] Failed to extract text from {resume_path}: {err}")
    return ""

def ensure_resume_text(user_data, resume_path):
    user_data = dict(user_data or {})
    if not str(user_data.get("resume_text") or "").strip():
        resume_text = extract_resume_text_from_path(resume_path)
        if resume_text:
            user_data["resume_text"] = resume_text
            user_data["resume_text_source"] = "resume_path"
    return user_data

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
    if "paylocity" in host:
        return "paylocity"
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
    if ats == "paylocity":
        return "paylocity"
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
    has_workday_job_action = bool(re.search(
        r"\b(apply now|apply to job|apply for this job|start application|begin application|apply)\b",
        text_low,
    ))
    has_workday_terminal_message = bool(re.search(
        r"\b(job is no longer available|custom job error|no longer accepting|position is no longer available)\b",
        text_low,
    ))
    return {
        "url": url,
        "text_len": best_text_len,
        "controls": total_controls,
        "form_controls": total_form_controls,
        "has_apply_body": bool(re.search(r"\b(autofill with resume|apply manually|upload resume|create account|sign in|save and continue|review)\b", text_low)),
        "has_workday_job_action": has_workday_job_action,
        "has_workday_terminal_message": has_workday_terminal_message,
        "has_workday_loading_shell": "loading" in text_low and not has_workday_job_action and not has_workday_terminal_message,
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
        write_live_smoke_progress(
            page,
            action="readiness_wait",
            readiness_snapshot=last_snapshot,
        )
        parsed_url = urlparse(last_snapshot.get("url") or "")
        host = (parsed_url.hostname or "").lower()
        path = (parsed_url.path or "").lower()
        is_workday = "myworkdayjobs.com" in host
        is_workday_apply_flow = is_workday and (path.endswith("/apply") or "/apply/" in path)
        if last_snapshot.get("form_controls", 0) > 0:
            write_live_smoke_progress(page, action="readiness_ready", readiness_snapshot=last_snapshot)
            return {"ready": True, **last_snapshot}
        if is_workday_apply_flow:
            text_low = (last_snapshot.get("text_preview") or "").lower()
            if last_snapshot.get("has_apply_body") or any(
                token in text_low for token in ["job is no longer available", "custom job error", "no longer accepting"]
            ):
                write_live_smoke_progress(page, action="readiness_ready", readiness_snapshot=last_snapshot)
                return {"ready": True, **last_snapshot}
        elif is_workday:
            if last_snapshot.get("has_workday_job_action") or last_snapshot.get("has_workday_terminal_message"):
                write_live_smoke_progress(page, action="readiness_ready", readiness_snapshot=last_snapshot)
                return {"ready": True, **last_snapshot}
        elif last_snapshot.get("controls", 0) >= 2 or last_snapshot.get("text_len", 0) >= 120:
            write_live_smoke_progress(page, action="readiness_ready", readiness_snapshot=last_snapshot)
            return {"ready": True, **last_snapshot}
        if (
            (allow_reload or is_workday_apply_flow)
            and not reloaded
            and (time.time() - start) > 20
            and (
                (last_snapshot.get("text_len", 0) < 20 and last_snapshot.get("controls", 0) == 0)
                or (is_workday_apply_flow and not last_snapshot.get("has_apply_body") and last_snapshot.get("form_controls", 0) == 0)
            )
        ):
            print(f"[Access Gate] Page still not ready after 20s; refreshing once. snapshot={last_snapshot}")
            try:
                page.reload(wait_until="domcontentloaded", timeout=60000)
                reloaded = True
            except Exception as reload_err:
                print(f"[Access Gate] Refresh failed while waiting for apply page: {reload_err}")
        page.wait_for_timeout(2000)
    print(f"[Access Gate] Page readiness timeout after {timeout}ms. snapshot={last_snapshot}")
    return {"ready": False, **last_snapshot}

def is_workday_blank_apply_step(page, fields=None):
    parsed = urlparse(page.url or "")
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if "myworkdayjobs.com" not in host or not (path.endswith("/apply") or "/apply/" in path):
        return False
    if fields:
        visible_fields = [
            field for field in fields
            if field.get("input_type") not in {"hidden", "submit", "button"}
        ]
        if visible_fields:
            return False
    text = page_body_text(page, timeout=1500).lower()
    if (
        "follow us" in text
        and "applicant privacy policy" in text
        and not any(token in text for token in ["save and continue", "submit application", "select one", "phone number", "upload resume", "job title"])
    ):
        return True
    if not all(token in text for token in ["my information", "my experience", "review"]):
        return False
    if any(token in text for token in ["save and continue", "submit application", "select one", "phone number", "upload resume"]):
        return False
    return True

def recover_workday_blank_apply_step(page, timeout=30000):
    if not is_workday_blank_apply_step(page):
        return {"attempted": False, "reason": "not_blank_workday_step"}
    for attempt in range(2):
        page.wait_for_timeout(5000)
        fields = extract_form_schema(page, {})
        if not is_workday_blank_apply_step(page, fields):
            return {"attempted": True, "recovered": True, "method": "wait", "attempt": attempt + 1}
    try:
        page.reload(wait_until="domcontentloaded", timeout=60000)
        readiness = wait_for_apply_page_ready(page, timeout=timeout, allow_reload=False)
        fields = extract_form_schema(page, {})
        return {
            "attempted": True,
            "recovered": not is_workday_blank_apply_step(page, fields),
            "method": "reload",
            "readiness": readiness,
        }
    except Exception as err:
        return {"attempted": True, "recovered": False, "method": "reload", "error": str(err)}

def wait_for_workday_step_interactive(page, timeout=18000):
    host = (urlparse(page.url or "").hostname or "").lower()
    if "myworkdayjobs.com" not in host:
        return {"waited": False, "reason": "not_workday"}
    deadline = time.time() + (timeout / 1000.0)
    last_state = {}
    while time.time() < deadline:
        try:
            state = page.evaluate("""
                () => {
                  const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
                  const visible = el => {
                    if (!el || !el.isConnected) return false;
                    const style = window.getComputedStyle(el);
                    const box = el.getBoundingClientRect();
                    return !!(box.width && box.height) && style.visibility !== "hidden" && style.display !== "none";
                  };
                  const buttons = Array.from(document.querySelectorAll("button, [role='button'], input[type='button'], input[type='submit']"))
                    .filter(visible)
                    .map(el => ({
                      text: clean(el.innerText || el.textContent || el.value || el.getAttribute("aria-label") || ""),
                      disabled: !!el.disabled || el.getAttribute("aria-disabled") === "true"
                    }));
                  const save = buttons.find(item => /save and continue/i.test(item.text));
                  const busy = Array.from(document.querySelectorAll("[aria-busy='true'], [role='progressbar'], [class*='skeleton' i], [class*='loading' i]"))
                    .filter(visible).length;
                  return { hasSave: !!save, saveDisabled: !!(save && save.disabled), busy };
                }
            """)
            last_state = state
            if state.get("hasSave") and not state.get("saveDisabled") and not state.get("busy"):
                return {"waited": True, "ready": True, "state": state}
            if not state.get("hasSave") and not state.get("busy"):
                return {"waited": True, "ready": True, "state": state}
        except Exception as err:
            last_state = {"error": str(err)}
        page.wait_for_timeout(1000)
    return {"waited": True, "ready": False, "state": last_state}

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
    key_compact = re.sub(r"[^a-z0-9]+", "", key)
    if input_type in {"submit", "button", "image", "reset"}:
        return True
    if input_type == "select" and ("languageselector" in key_compact or key_compact in {"english", "language"}):
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

def field_answer_override(label, user_data):
    label_low = (label or "").lower()
    field_answers = user_data.get("field_answers") or {}
    normalized_label = re.sub(r"[^a-z0-9]+", " ", label_low).strip()
    label_key = label_low.replace(" ", "_")
    generic_label = normalized_label in {
        "company",
        "employer",
        "organization",
        "location",
        "title",
        "job title",
        "month",
        "year",
        "from",
        "to",
        "start",
        "end",
    }
    for key, answer in field_answers.items():
        if not isinstance(answer, dict):
            continue
        value = answer.get("value")
        if value in (None, "") or answer.get("type") == "file":
            continue
        raw_field = str(answer.get("field") or "")
        normalized_field = re.sub(r"[^a-z0-9]+", " ", raw_field.lower()).strip()
        common_key = str(answer.get("common_key") or "")
        if common_key == "phone" and any(token in label_low for token in [
            "country phone code", "country code", "phone device", "phone type", "device type", "extension"
        ]):
            continue
        if common_key == "phone_country_code" and not any(token in label_low for token in [
            "country phone code", "phone country code", "country calling code"
        ]):
            continue
        if common_key == "country" and "country phone" in label_low:
            continue
        if normalized_field and (
            normalized_label == normalized_field
            or (not generic_label and (normalized_label in normalized_field or normalized_field in normalized_label))
        ):
            return str(value)
        if common_key == "phone_country_code" and any(token in label_low for token in [
            "country phone code", "phone country code", "country calling code"
        ]):
            return str(value)
        if common_key and (label_key == common_key or label_key.endswith(f"_{common_key}") or label_key.startswith(f"{common_key}_")):
            return str(value)
    return None

def is_previous_worker_question(label):
    label_low = (label or "").lower()
    return bool(
        "ever been employed" in label_low
        or "previously employed" in label_low
        or "previously been employed" in label_low
        or "former employee" in label_low
        or "employee or contractor" in label_low
    )

def candidate_profile_value(label, input_type, user_data):
    label_low = (label or "").lower()
    override = field_answer_override(label, user_data)
    if override:
        return override

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
    if "country phone code" in label_low or "phone country code" in label_low or "country calling code" in label_low:
        return pick("phone_country_code", "country_phone_code", "calling_code") or "United States"
    if "extension" in label_low:
        return pick("phone_extension")
    if "phone device" in label_low or "phone type" in label_low or "device type" in label_low:
        return pick("phone_device_type")
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
    if normalized_option_text(label_low) in {"language", "languages"}:
        return pick("language", "primary_language")
    if normalized_option_text(label_low) in {"overall", "overall proficiency", "proficiency", "level"}:
        return pick("language_overall", "language_proficiency", "primary_language_proficiency")
    if "how did you hear" in label_low or re.search(r"\bsource\b", label_low):
        return pick("how_heard", "source", "referral_source")
    if is_previous_worker_question(label_low):
        return pick("previous_worker", "previous_employee", "former_employee") or "No"
    if re.search(r"\b18\s+years?\b|\b18\s+or\s+older\b|over\s+18|at\s+least\s+18", label_low):
        return pick("age_over_18")
    if is_business_conflict_disclosure_question(label_low):
        return pick("business_conflict_disclosure", "conflict_disclosure", "government_relationship")
    if "notice period" in label_low:
        return pick("notice_period", "start_date", "earliest_start_date")
    if "start date" in label_low or "available to start" in label_low or "earliest start" in label_low or re.search(r"\bwhen\b.*\bstart\b", label_low):
        return pick("start_date", "earliest_start_date", "notice_period")
    if "street" in label_low or "address" in label_low:
        if "other" in label_low or "address 2" in label_low or "address2" in label_low:
            return pick("address2", "street_address_2")
        if "address line 1" in label_low or "address1" in label_low or "street" in label_low:
            return pick("address1", "street_address")
        return pick("address1", "street_address", "address")
    if "city" in label_low:
        return pick("city")
    if "country" in label_low:
        return pick("country") or "United States"
    if "state" in label_low or "province" in label_low or "region" in label_low:
        return pick("state", "province", "region")
    if "zip" in label_low or "postal" in label_low:
        return pick("zip", "zipcode", "postal_code")
    if "school" in label_low or "university" in label_low or "institution" in label_low or "college" in label_low:
        return pick("education_school")
    if "field of study" in label_low or "area of study" in label_low or "major" in label_low or "discipline" in label_low:
        return pick("education_field")
    if "degree" in label_low or "education level" in label_low or "level of education" in label_low:
        return pick("education_degree")
    if "graduation month" in label_low or "end month" in label_low or "to month" in label_low:
        return pick("education_end_month")
    if "graduation year" in label_low or "end year" in label_low or "to year" in label_low:
        return pick("education_end_year")
    return None

def is_business_conflict_disclosure_question(label_low):
    if not label_low:
        return False
    excluded = [
        "authorized to work",
        "legally authorized",
        "work authorization",
        "sponsorship",
        "sponsor",
        "visa",
        "gender",
        "sex",
        "race",
        "ethnicity",
        "hispanic",
        "veteran",
        "disability",
        "voluntary self",
        "self-identification",
        "self identification",
        "eeo",
        "equal opportunity",
    ]
    if any(token in label_low for token in excluded):
        return False
    groups = [
        ["government", "contractor"],
        ["government", "employee"],
        ["government", "official"],
        ["business relationship"],
        ["family member"],
        ["relative"],
        ["conflict of interest"],
        ["limited liability partnership"],
        [" llp"],
        ["procurement"],
        ["contracting official"],
        ["personal services"],
        ["organization in which"],
        ["spouse"],
        ["parent"],
        ["child"],
        ["sibling"],
    ]
    return any(all(token in label_low for token in group) for group in groups)

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
    if "notice period" in text:
        return "notice_period"
    if "start date" in text or "available to start" in text or "earliest start" in text:
        return "start_date"
    if "street" in text or "address" in text:
        return "address"
    if "city" in text:
        return "city"
    if "country" in text:
        return "country"
    if "state" in text or "province" in text or "region" in text:
        return "state_region"
    if "zip" in text or "postal" in text:
        return "postal_code"
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
        return el.closest("fieldset, [role='group'], [role='radiogroup'], .fieldcontain, [class*='question-'][class*='container'], .question, .form-group, [data-question], [data-field-container], section, li");
      };
      const groupTextFor = (el, labels) => {
        const container = groupContainerFor(el);
        if (!container) return "";
        const text = cleanText(container.innerText);
        const optionText = cleanText(labels.join(" "));
        if (!text || text === optionText || text.length > 900) return "";
        return text;
      };
      const groupLabelFor = (container) => {
        if (!container) return "";
        const node = container.querySelector("legend, :scope > label, :scope > .label, :scope > [data-label]");
        return node ? cleanText(node.innerText || node.textContent) : "";
      };
      const isSelectLike = (el) => {
        const tag = el.tagName.toLowerCase();
        const role = (el.getAttribute("role") || "").toLowerCase();
        const popup = (el.getAttribute("aria-haspopup") || "").toLowerCase();
        const automation = (el.getAttribute("data-automation-id") || "").toLowerCase();
        return tag === "select" || role === "combobox" || popup === "listbox" ||
          el.hasAttribute("aria-expanded") || /prompt|select|dropdown|combobox/.test(automation);
      };
      const isPlaceholderSelectText = (text) => /^(select one|select one required|choose|choose one|choose an answer|choose an option|select|required|search)?$/i.test(cleanText(text));
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
        const type = (el.getAttribute("type") || "").toLowerCase();
        if ((type === "radio" || type === "checkbox") && name && el.getAttribute("value")) {
          return `${tag}[name="${esc(name)}"][value="${esc(el.getAttribute("value"))}"]`;
        }
        if (name) return `${tag}[name="${esc(name)}"]`;
        return `${tag}:nth-of-type(${index + 1})`;
      };
        return elements.map((el, index) => {
          const tag = el.tagName.toLowerCase();
          const type = isSelectLike(el) ? "select" : (el.getAttribute("type") || tag).toLowerCase();
          const groupContainer = groupContainerFor(el);
          const groupText = groupContainer ? cleanText(groupContainer.innerText) : "";
          const groupLabel = groupLabelFor(groupContainer);
          const name = el.getAttribute("name") || "";
          const radioGroupChecked = type === "radio" && name
            ? !!document.querySelector(`input[type="radio"][name="${esc(name)}"]:checked`)
            : !!el.checked;
          const ownValue = cleanText(el.value || el.innerText || el.textContent || el.getAttribute("aria-label") || "");
          const options = tag === "select"
            ? Array.from(el.options || []).map(option => option.innerText.trim()).filter(Boolean).slice(0, 40)
            : [];
          return {
            label: labelFor(el),
            tag,
            input_type: type,
            name,
            id: el.getAttribute("id") || "",
            placeholder: el.getAttribute("placeholder") || "",
            required: !!el.required || el.getAttribute("aria-required") === "true" || el.getAttribute("data-required") === "true" ||
              /\\*/.test(labelFor(el)) || ((type === "radio" || type === "checkbox" || type === "select") && /(^|\\s|\\*)Required\\b/i.test([labelFor(el), ownValue, el.getAttribute("aria-label") || ""].join(" "))),
            disabled: !!el.disabled,
            read_only: !!el.readOnly,
            value_present: type === "radio" ? radioGroupChecked : (type === "checkbox" ? !!el.checked : (type === "select" ? !!ownValue && !isPlaceholderSelectText(ownValue) : !!el.value)),
            value: ownValue || "",
            checked: !!el.checked,
            options,
            selector: selectorFor(el, index),
            group_text: groupText,
            group_label: groupLabel,
            data_question: groupContainer ? cleanText(groupContainer.getAttribute("data-question")) : "",
            data_field: cleanText(el.getAttribute("data-field") || (groupContainer && groupContainer.getAttribute("data-field"))),
          visible: isVisible(el)
        };
      }).filter(item => item.visible && !item.disabled);
    }
    """
    for scope_index, scope in enumerate(get_apply_scopes(page)):
        try:
            locator = scope.locator("input, textarea, select, button[aria-haspopup], button[aria-expanded], [role='combobox']")
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

def page_has_visible_action_matching(page, patterns):
    compiled = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    try:
        texts = page.locator(
            "button, a, [role='button'], input[type='button'], input[type='submit']"
        ).evaluate_all(
            """
            (elements) => elements
              .filter((el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return !!(rect.width || rect.height || el.getClientRects().length) &&
                  style.visibility !== "hidden" && style.display !== "none" &&
                  !el.disabled && el.getAttribute("aria-disabled") !== "true";
              })
              .map((el) => [
                el.innerText,
                el.textContent,
                el.getAttribute("aria-label"),
                el.getAttribute("value"),
                el.getAttribute("title")
              ].filter(Boolean).join(" "))
            """
        )
    except Exception:
        return False
    for text in texts:
        label = re.sub(r"\s+", " ", str(text or "")).strip()
        if label and any(pattern.search(label) for pattern in compiled):
            return True
    return False

def infer_apply_stage(page, fields):
    text = page_body_text(page)[:16000].lower()
    url = page.url.lower()
    try:
        title = (page.title(timeout=1000) or "").lower()
    except Exception:
        title = ""
    try:
        heading_text = compact_text(page.locator('h1, h2, h3, [role="heading"], [data-automation-id*="pageHeader"]').evaluate_all("""
            nodes => nodes
              .filter(node => {
                const box = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return !!(box.width && box.height) && style.visibility !== "hidden" && style.display !== "none";
              })
              .map(node => node.innerText || node.textContent || "")
              .join(" ")
        """)).lower()
    except Exception:
        heading_text = ""
    password_count = sum(1 for f in fields if f.get("input_type") == "password")
    code_count = sum(
        1 for f in fields
        if re.search(r"(verification|one[-\s]?time|otp|pin|security\s+code)", f"{f.get('label','')} {f.get('name','')} {f.get('id','')}", re.IGNORECASE)
    )
    visible_field_count = len([f for f in fields if f.get("input_type") not in {"hidden", "submit", "button"}])

    if "captcha" in text or "recaptcha" in text or "hcaptcha" in text:
        return "blocked_captcha"
    if "my information" in heading_text or ("my information" in text and "how did you hear about us" in text):
        return "my_information"
    if "my experience" in heading_text or "type to add skills" in text:
        return "my_experience"
    if "voluntary disclosures" in heading_text or "personal data statement" in text:
        return "voluntary_disclosures"
    if "self identify" in heading_text or "voluntary self-identification" in text or "self-identification of disability" in text:
        return "self_identify"
    if "review" in heading_text and ("application" in text or "submit" in text):
        return "review"
    if "application questions" in heading_text:
        return "application_questions"
    has_consent_control = page_has_visible_action_matching(page, [
        r"accept\s+(all|cookies)", r"^accept$", r"^agree$", r"i agree", r"consent", r"acknowledge"
    ])
    if ("we use cookies" in text or "tracking technologies" in text or "cookie" in text) and has_consent_control:
        return "privacy_policy"
    if ("privacy policy" in text or "terms of use" in text or "data protection" in text) and has_consent_control:
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
        create_account_indicators = [
            "verify new password",
            "confirm password",
            "password requirements",
            "create an account to apply",
            "please create an account",
        ]
        if password_count >= 2 or any(token in text for token in create_account_indicators):
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
        if is_protected_workday_country_field(field):
            current_country = protected_country_visible_value(page, field)
            if is_us_country_value(current_country):
                skipped.append({"field": key, "reason": "already_has_protected_country", "risk": field.get("risk")})
                continue
            expected_country = profile_country_value(user_data)
            if expected_country and req.allow_low_risk_autofill:
                fill_plan.append({
                    "action_type": "select_option",
                    "field_id": field.get("field_id"),
                    "canonical_field": "country",
                    "value": "United States of America" if is_us_country_value(expected_country) else expected_country,
                    "answer_source": "profile_protected_country",
                    "confidence": 1.0,
                    "risk_level": field.get("risk"),
                    "locator_candidates": field.get("locator_candidates", []),
                    "requires_confirmation": False,
                })
                continue
            blocking_issues.append({
                "type": "protected_country_missing_profile_country",
                "field": key,
                "field_id": field.get("field_id"),
                "canonical_field": "country",
                "risk": "medium",
                "reason": "Protected Workday country field requires an explicit profile country.",
            })
            continue
        if field.get("value_present") or (input_type in {"checkbox", "radio"} and field.get("checked")):
            skipped.append({"field": key, "reason": "already_has_value", "risk": field.get("risk")})
            continue
        confirmed_answer = field_answer_override(field.get("label"), user_data)
        if confirmed_answer and req.allow_low_risk_autofill:
            if input_type == "radio" and not answer_matches_field_option(field, confirmed_answer):
                skipped.append({"field": key, "reason": "saved_answer_for_sibling_radio_option", "risk": field.get("risk")})
                continue
            fill_plan.append({
                "action_type": "check_field" if input_type == "checkbox" else ("select_option" if input_type in {"radio", "select"} else "fill_field"),
                "field_id": field.get("field_id"),
                "canonical_field": field.get("canonical_field"),
                "value": confirmed_answer,
                "answer_source": "saved_field_answer",
                "confidence": 0.99,
                "risk_level": field.get("risk"),
                "locator_candidates": field.get("locator_candidates", []),
                "requires_confirmation": False,
            })
            continue
        if field.get("risk") == "high":
            if input_type == "checkbox" and not field.get("required"):
                skipped.append({"field": key, "reason": "optional_high_risk_checkbox_left_for_user_or_default_off", "risk": field.get("risk")})
                continue
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
              const automationLabel = el.getAttribute("data-automation-label");
              if (automationLabel) return automationLabel;
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

def click_workday_sign_in_submit(page):
    selectors = [
        'button[data-automation-id*="signIn" i]',
        'button[data-automation-id*="sign-in" i]',
        'button:has-text("Sign In")',
        'button:has-text("Log In")',
        'input[type="submit"][value*="Sign" i]',
    ]
    for scope in get_apply_scopes(page):
        for selector in selectors:
            try:
                controls = scope.locator(selector)
                for index in range(min(controls.count(), 5)):
                    locator = controls.nth(index)
                    if not locator.is_visible(timeout=800):
                        continue
                    label = locator_label(locator) or "Sign In"
                    if FINAL_SUBMIT_RE.search(label or ""):
                        continue
                    locator.click(timeout=4000, force=True)
                    page.wait_for_timeout(5000)
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

def workday_resume_autofill_allowed(req, history):
    if not (getattr(req, "allow_resume_upload", False) and getattr(req, "resume_path", "") and os.path.exists(getattr(req, "resume_path", ""))):
        return False
    return not any(item.get("autofill_blank_timeout") for item in history or [])

def is_blank_workday_autofill_branch(page, fields=None):
    parsed = urlparse(page.url or "")
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if "myworkdayjobs.com" not in host or "autofillwithresume" not in path:
        return False
    if fields:
        visible_fields = [
            field for field in fields
            if field.get("input_type") not in {"hidden", "submit", "button"}
        ]
        if visible_fields:
            return False
    text = page_body_text(page, timeout=1500).lower()
    return "my information" in text and "my experience" in text and "save and continue" not in text

def is_workday_autofill_resume_url(url):
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    return "myworkdayjobs.com" in host and "autofillwithresume" in path

def try_workday_autofill_resume_upload(page, resume_path):
    if not resume_path or not os.path.exists(resume_path):
        return False, "resume_path_missing"
    upload_label_re = re.compile(
        r"\b(upload|select|choose|attach|browse)\b.*\b(resume|cv|file)\b|"
        r"\b(resume|cv|file)\b.*\b(upload|select|choose|attach|browse)\b|"
        r"^\s*choose file\s*$",
        re.IGNORECASE,
    )
    dropzone_re = re.compile(r"\b(drop|drag|upload|attach|resume|cv|file)\b", re.IGNORECASE)
    for scope in get_apply_scopes(page):
        try:
            file_inputs = scope.locator('input[type="file"]')
            for index in range(min(file_inputs.count(), 10)):
                file_input = file_inputs.nth(index)
                file_input.set_input_files(resume_path, timeout=5000)
                page.wait_for_timeout(3000)
                return True, "input[type=file]"
        except Exception:
            pass

    selectors = [
        'button, a, [role="button"], input[type="button"], input[type="submit"]',
        '[aria-label*="upload" i], [aria-label*="resume" i], [aria-label*="file" i]',
        '[data-automation-id*="upload" i], [data-automation-id*="resume" i], [data-automation-id*="file" i]',
        '[class*="dropzone" i], [class*="drop-zone" i], [class*="file" i]',
    ]
    last_error = ""
    for scope in get_apply_scopes(page):
        for selector in selectors:
            try:
                controls = scope.locator(selector)
                for index in range(min(controls.count(), 80)):
                    locator = controls.nth(index)
                    try:
                        if not locator.is_visible(timeout=300):
                            continue
                        label = locator_label(locator)
                        attrs = locator.evaluate("""el => [
                          el.getAttribute('aria-label'),
                          el.getAttribute('data-automation-id'),
                          el.getAttribute('class'),
                          el.getAttribute('id')
                        ].filter(Boolean).join(' ')""")
                        searchable = " ".join([label or "", attrs or ""])
                        if not upload_label_re.search(searchable) and not dropzone_re.search(searchable):
                            continue
                        if FINAL_SUBMIT_RE.search(searchable):
                            continue
                        with page.expect_file_chooser(timeout=5000) as fc_info:
                            locator.click(timeout=3000, force=True)
                        fc_info.value.set_files(resume_path)
                        page.wait_for_timeout(3000)
                        method_label = compact_text(label or attrs or selector)[:120]
                        return True, f"file_chooser:{method_label}"
                    except Exception as err:
                        last_error = str(err)
                        continue
            except Exception:
                continue
    return False, last_error or "workday_autofill_upload_ui_not_found"

def workday_autofill_upload_success_marker(page, resume_path=""):
    filename = os.path.basename(resume_path or "").lower()
    text = page_body_text(page, timeout=1500).lower()
    if any(marker in text for marker in ["successfully uploaded", "upload complete", "resume uploaded"]):
        return True
    if filename and filename in text:
        return True
    tile_selectors = [
        '[data-automation-id*="file" i]',
        '[data-automation-id*="attachment" i]',
        '[class*="file" i]',
        '[class*="attachment" i]',
        '[aria-label*="file" i]',
        '[aria-label*="resume" i]',
    ]
    for scope in get_apply_scopes(page):
        for selector in tile_selectors:
            try:
                tiles = scope.locator(selector)
                for index in range(min(tiles.count(), 20)):
                    tile = tiles.nth(index)
                    if not tile.is_visible(timeout=300):
                        continue
                    label = locator_label(tile).lower()
                    if "pdf" in label or "resume" in label or (filename and filename in label):
                        return True
            except Exception:
                continue
    return False

def workday_autofill_ready_state(page, user_data=None):
    fields = extract_form_schema(page, user_data or {})
    stage = infer_apply_stage(page, fields)
    blank = is_blank_workday_autofill_branch(page, fields)
    path = (urlparse(page.url or "").path or "").lower()
    away_from_autofill = "autofillwithresume" not in path
    ready_stages = {"my_information", "my_experience", "application_form"}
    ready = bool(fields) or (away_from_autofill and stage in ready_stages)
    return {
        "ready": ready,
        "url": page.url,
        "stage": stage,
        "field_count": len(fields),
        "blank": blank,
        "away_from_autofill": away_from_autofill,
    }

def click_workday_post_upload_continue(page):
    return click_matching_control(
        page,
        [
            r"^\s*continue\s*$",
            r"^\s*next\s*$",
            r"\bsave\s*(and|&)\s*continue\b",
            r"\bstart application\b",
        ],
        skip_final_submit=True,
        avoid_patterns=[
            r"^\s*submit\s*$",
            r"^\s*send\s*$",
            r"^\s*complete\s*$",
            r"^\s*finish\s*$",
            r"\bsubmit application\b",
            r"\bsend application\b",
            r"\bcomplete application\b",
            r"\bfinish application\b",
            r"\bfinal submit\b",
        ],
    )

def continue_after_workday_resume_upload(page, req, method="", timeout_ms=45000):
    resume_path = getattr(req, "resume_path", "")
    success_marker = workday_autofill_upload_success_marker(page, resume_path)
    screenshot_path = capture_apply_screenshot(page, "workday_autofill_uploaded") if success_marker else ""
    page.wait_for_timeout(2500)

    state = workday_autofill_ready_state(page, {})
    if state["ready"]:
        return {
            "attempted": True,
            "uploaded": True,
            "success_marker": success_marker,
            "continued_after_upload": False,
            "ready": True,
            "field_count": int(state.get("field_count") or 0),
            "stage": state.get("stage"),
            "method": method,
            "reason": "workday_autofill_form_ready_after_upload",
            "screenshot_path": screenshot_path,
            "autofill_resume_wait": {"ready": True, **state},
        }

    clicked, label = click_workday_post_upload_continue(page)
    if not clicked:
        screenshot_path = screenshot_path or capture_apply_screenshot(page, "workday_autofill_continue_not_found")
        return {
            "attempted": True,
            "uploaded": True,
            "success_marker": success_marker,
            "continued_after_upload": False,
            "ready": False,
            "field_count": int(state.get("field_count") or 0),
            "stage": state.get("stage"),
            "method": method,
            "reason": "workday_autofill_continue_not_found",
            "autofill_blank_timeout": True,
            "screenshot_path": screenshot_path,
            "autofill_resume_wait": {"ready": False, **state},
        }

    deadline = time.time() + (timeout_ms / 1000.0)
    last_state = state
    while time.time() < deadline:
        last_state = workday_autofill_ready_state(page, {})
        if last_state["ready"]:
            return {
                "attempted": True,
                "uploaded": True,
                "success_marker": success_marker,
                "continued_after_upload": True,
                "continue_label": label,
                "ready": True,
                "field_count": int(last_state.get("field_count") or 0),
                "stage": last_state.get("stage"),
                "method": method,
                "reason": "workday_autofill_continue_reached_form",
                "screenshot_path": screenshot_path,
                "autofill_resume_wait": {"ready": True, **last_state},
            }
        page.wait_for_timeout(2000)

    screenshot_path = capture_apply_screenshot(page, "workday_autofill_continue_parse_timeout")
    return {
        "attempted": True,
        "uploaded": True,
        "success_marker": success_marker,
        "continued_after_upload": True,
        "continue_label": label,
        "ready": False,
        "field_count": int(last_state.get("field_count") or 0),
        "stage": last_state.get("stage"),
        "method": method,
        "reason": "workday_autofill_continue_parse_timeout",
        "autofill_blank_timeout": True,
        "screenshot_path": screenshot_path,
        "autofill_resume_wait": {"ready": False, **last_state},
    }

def handle_workday_autofill_with_resume_branch(page, req):
    parsed = urlparse(page.url or "")
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if "myworkdayjobs.com" not in host or "autofillwithresume" not in path:
        return {"attempted": False, "reason": "not_workday_autofill"}
    if not getattr(req, "allow_resume_upload", False):
        return {"attempted": False, "reason": "resume_upload_disabled"}
    resume_path = getattr(req, "resume_path", "")
    if not resume_path or not os.path.exists(resume_path):
        return {"attempted": False, "reason": "resume_path_missing"}

    timeout = 45000
    deadline = time.time() + (timeout / 1000.0)
    last_state = {}
    uploaded = False
    method = ""
    while time.time() < deadline:
        fields = extract_form_schema(page, {})
        stage = infer_apply_stage(page, fields)
        blank = is_blank_workday_autofill_branch(page, fields)
        last_state = {
            "url": page.url,
            "stage": stage,
            "field_count": len(fields),
            "blank": blank,
        }
        if uploaded or workday_autofill_upload_success_marker(page, resume_path):
            return continue_after_workday_resume_upload(page, req, method or "already_uploaded", timeout)
        if not uploaded:
            ok, upload_method = try_workday_autofill_resume_upload(page, resume_path)
            if ok:
                uploaded = True
                method = upload_method
                return continue_after_workday_resume_upload(page, req, method, timeout)
        page.wait_for_timeout(2000)

    reason = "workday_autofill_parse_timeout" if uploaded else "workday_autofill_upload_ui_not_found"
    screenshot_path = capture_apply_screenshot(page, reason)
    return {
        "attempted": True,
        "uploaded": uploaded,
        "ready": False,
        "field_count": int(last_state.get("field_count") or 0),
        "method": method,
        "reason": reason,
        "autofill_blank_timeout": True,
        "screenshot_path": screenshot_path,
        "autofill_resume_wait": {
            "ready": False,
            "field_count": int(last_state.get("field_count") or 0),
            "state": last_state,
        },
    }

def wait_for_workday_autofill_resume_result(page, user_data=None, timeout=45000):
    if not is_workday_autofill_resume_url(page.url):
        return {"waited": False, "reason": "not_workday_autofill"}
    deadline = time.time() + (timeout / 1000.0)
    last_state = {}
    while time.time() < deadline:
        fields = extract_form_schema(page, user_data or {})
        stage = infer_apply_stage(page, fields)
        blank = is_blank_workday_autofill_branch(page, fields)
        last_state = {
            "url": page.url,
            "stage": stage,
            "field_count": len(fields),
            "blank": blank,
        }
        if fields or not blank:
            return {"waited": True, "ready": True, "field_count": len(fields), "state": last_state}
        page.wait_for_timeout(2000)
    return {"waited": True, "ready": False, "field_count": int(last_state.get("field_count") or 0), "state": last_state}

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
    resume_name = os.path.basename(req.resume_path)
    if resume_name and resume_name.lower() in page_body_text(page, timeout=1000).lower():
        return {"attempted": False, "uploaded": True, "reason": "resume_already_uploaded", "file": resume_name}
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

VISION_ACCESS_TRANSITIONS = {
    "click_apply": {
        "patterns": [r"^apply$", r"\bapply now\b", r"\bapply to job\b"],
        "avoid": [r"easy apply", r"submit", r"send", r"complete", r"finish"],
    },
    "click_apply_manually": {
        "patterns": [r"\bapply manually\b"],
        "avoid": [r"submit", r"send", r"complete", r"finish"],
    },
    "click_autofill_with_resume": {
        "patterns": [r"\bautofill with resume\b", r"\bapply with resume\b"],
        "avoid": [r"submit", r"send", r"complete", r"finish"],
    },
    "click_sign_in": {
        "patterns": [r"^sign in$", r"^log in$", r"^login$"],
        "avoid": [r"single sign", r"sso", r"linkedin", r"google", r"facebook"],
    },
    "click_create_account": {
        "patterns": [r"\bcreate account\b", r"\bcreate profile\b", r"\bnew user\b", r"\bregister\b", r"\bsign up\b"],
        "avoid": [r"submit", r"send", r"complete", r"finish"],
    },
    "click_continue": {
        "patterns": [r"^\s*continue\s*$", r"\bcontinue applying\b"],
        "avoid": [r"submit", r"send", r"complete", r"finish"],
    },
    "click_next": {
        "patterns": [r"^\s*next\s*$"],
        "avoid": [r"submit", r"send", r"complete", r"finish"],
    },
    "click_save_and_continue": {
        "patterns": [r"\bsave\s*(and|&)\s*continue\b", r"\bsave\s*/\s*continue\b"],
        "avoid": [r"submit", r"send", r"complete", r"finish"],
    },
}

VISION_STOP_TRANSITIONS = {
    "stop_final_submit_guard": "final_submit_guard",
    "stop_human_required": "human_required",
    "stop_captcha": "captcha",
    "stop_blocked": "blocked",
    "stop_no_safe_action": "no_safe_action",
}

def visible_navigation_controls(page, limit=30):
    controls = []
    for scope in get_apply_scopes(page):
        try:
            locators = scope.locator('button, a, [role="button"], input[type="button"], input[type="submit"]')
            for index in range(min(locators.count(), limit)):
                locator = locators.nth(index)
                try:
                    if not locator.is_visible(timeout=300):
                        continue
                    label = locator_label(locator)
                    if not label:
                        continue
                    controls.append({
                        "label": label[:160],
                        "is_final_submit": bool(FINAL_SUBMIT_RE.search(label) or BARE_FINAL_SUBMIT_RE.search(label)),
                    })
                    if len(controls) >= limit:
                        return controls
                except Exception:
                    continue
        except Exception:
            continue
    return controls

def classify_visual_access_state(page, req, reason="unknown"):
    if not client:
        return {"ok": False, "reason": "vision_client_unavailable"}

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
    visible_controls = visible_navigation_controls(page)
    image_part = types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png")
    prompt = f"""
You are classifying a job application PRECHECK browser state. You do not control the browser.

Current URL: {page.url}
Reason for visual fallback: {reason}
Screenshot resolution: {img_w}x{img_h} physical pixels.
Resume PDF available for upload: {has_resume}
Visible controls detected by deterministic code:
{json.dumps(visible_controls, ensure_ascii=False)}
Page text excerpt:
{page_text}

Classify the page and suggest exactly one high-level transition. The browser state machine will validate it
against an allowlist and run deterministic handlers. Do not provide coordinates. Do not request arbitrary clicks.

Allowed suggested_transition values:
- click_apply
- click_apply_manually
- click_autofill_with_resume
- click_sign_in
- click_create_account
- click_continue
- click_next
- click_save_and_continue
- upload_resume
- scroll_down
- scroll_up
- wait
- stop_final_submit_guard
- stop_human_required
- stop_captcha
- stop_blocked
- stop_no_safe_action

Hard safety rules:
- NEVER suggest submitting final controls: Submit Application, Send Application, Complete Application, Finish Application, Final Submit.
- If the page is ready for final submission, return suggested_transition "stop_final_submit_guard".
- Do not invent answers to sensitive questions. This visual fallback is for navigation/upload only.
- Prefer click_autofill_with_resume when a resume PDF is available. Prefer click_apply_manually only when no resume upload path is visible.

Return ONLY valid JSON:
{{
  "observed_state": "job_detail|application_choice|sign_in|create_account|resume_upload|application_form|loading|captcha|blocked|final_review|unknown",
  "confidence": 0.0,
  "evidence": ["short visible evidence strings"],
  "suggested_transition": "one allowed suggested_transition value",
  "visible_controls": ["visible labels relevant to the suggestion"]
}}
"""
    try:
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=[image_part, prompt],
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0),
        )
        classification = parse_model_json(response.text)
    except Exception as err:
        return {"ok": False, "reason": f"vision_query_failed:{err}", "screenshot_path": screenshot_path}

    transition = str(classification.get("suggested_transition") or "").strip().lower()
    confidence = classification.get("confidence")
    try:
        confidence = float(confidence)
    except Exception:
        confidence = 0.0
    return {
        "ok": True,
        "observed_state": str(classification.get("observed_state") or "unknown"),
        "confidence": confidence,
        "evidence": classification.get("evidence") if isinstance(classification.get("evidence"), list) else [],
        "suggested_transition": transition,
        "visible_controls": classification.get("visible_controls") if isinstance(classification.get("visible_controls"), list) else [],
        "deterministic_visible_controls": visible_controls,
        "screenshot_path": screenshot_path,
    }

def execute_vision_suggested_transition(page, req, classification):
    transition = str((classification or {}).get("suggested_transition") or "").strip().lower()
    result = {
        "acted": False,
        "suggested_transition": transition,
        "observed_state": (classification or {}).get("observed_state"),
        "confidence": (classification or {}).get("confidence"),
        "evidence": (classification or {}).get("evidence") or [],
        "visible_controls": (classification or {}).get("visible_controls") or [],
        "screenshot_path": (classification or {}).get("screenshot_path"),
    }
    if not (classification or {}).get("ok"):
        result["reason"] = (classification or {}).get("reason") or "vision_classification_failed"
        return result
    if transition in VISION_STOP_TRANSITIONS:
        result["stop_reason"] = VISION_STOP_TRANSITIONS[transition]
        return result
    if transition not in {"wait", "scroll_down", "scroll_up"} and page_has_final_submit(page):
        result["stop_reason"] = "final_submit_guard"
        result["reason"] = "deterministic final submit guard blocked vision transition"
        return result
    if transition == "wait":
        page.wait_for_timeout(5000)
        result["acted"] = True
        return result
    if transition in {"scroll_down", "scroll_up"}:
        page.evaluate(f"window.scrollBy(0, {600 if transition == 'scroll_down' else -600})")
        page.wait_for_timeout(1500)
        result["acted"] = True
        return result
    if transition == "upload_resume":
        ok, method = upload_resume_via_visible_control(page, req.resume_path)
        if not ok:
            ok, method = upload_resume_file(page, req.resume_path)
        result["acted"] = ok
        result["upload_method"] = method
        return result
    transition_def = VISION_ACCESS_TRANSITIONS.get(transition)
    if transition_def:
        clicked, label = click_matching_control(
            page,
            transition_def["patterns"],
            skip_final_submit=True,
            avoid_patterns=transition_def.get("avoid"),
        )
        result["acted"] = clicked
        result["element_label"] = label
        if not clicked:
            result["reason"] = "allowed_transition_control_not_found"
        return result

    result["reason"] = "vision_transition_not_allowed"
    return result

def visual_access_step(page, req, user_data, reason="unknown"):
    classification = classify_visual_access_state(page, req, reason=reason)
    result = execute_vision_suggested_transition(page, req, classification)
    result["vision_classification"] = classification
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

def fill_first_labeled_input(page, label_patterns, value, input_types=None):
    if not value:
        return False
    compiled = [re.compile(pattern, re.IGNORECASE) for pattern in label_patterns]
    allowed_types = {item.lower() for item in input_types} if input_types else None
    for scope in get_apply_scopes(page):
        try:
            locators = scope.locator("input, textarea")
            for index in range(min(locators.count(), 80)):
                locator = locators.nth(index)
                if not locator.is_visible(timeout=500):
                    continue
                input_type = (locator.get_attribute("type") or "text").lower()
                if allowed_types and input_type not in allowed_types:
                    continue
                label = locator_label(locator)
                if not label or not any(pattern.search(label) for pattern in compiled):
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
    email_filled = fill_first_available_scopes(page, [
        'input[name*="login" i]',
        'input[id*="login" i]',
        'input[name*="user" i]',
        'input[id*="user" i]',
        'input[type="email"]',
        'input[name*="email" i]',
        'input[id*="email" i]',
        'input[autocomplete="email"]'
    ], user_data.get("email"))
    if not email_filled:
        email_filled = fill_first_labeled_input(
            page,
            [r"\bemail\b", r"\be-mail\b"],
            user_data.get("email"),
            input_types={"text", "email"}
        )
    filled = {
        "email": email_filled,
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

def is_plain_phone_number_label(label, input_type=None):
    label_low = (label or "").lower()
    if any(token in label_low for token in [
        "country phone code",
        "phone country code",
        "country calling code",
        "phone device",
        "phone type",
        "device type",
        "extension",
    ]):
        return False
    return "phone number" in label_low or (
        input_type == "tel"
        and any(token in label_low for token in ["phone", "mobile", "cell"])
    )

def normalize_phone_number_value(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    digits = re.sub(r"\D+", "", text)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return digits
    return digits or text

def normalize_discovery_value(label, input_type, value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    label_low = (label or "").lower()
    if is_plain_phone_number_label(label, input_type):
        return normalize_phone_number_value(text)
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
    if "country" in label:
        return "United States"
    if "state" in label:
        return "California"
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

def semantic_field_text(field):
    return " ".join(str(field.get(key, "") or "") for key in [
        "label",
        "raw_label",
        "group_label",
        "group_text",
        "name",
        "id",
        "placeholder",
        "data_question",
        "data_field",
        "canonical_field",
    ]).strip()

def semantic_field_is_sensitive_or_compliance(field):
    text = semantic_field_text(field)
    if is_sensitive_question(text):
        return True
    return bool(re.search(
        r"(conflict of interest|export control|citizenship|permanent resident|government official|"
        r"authorized to work|work authorization|sponsor|visa)",
        text,
        re.IGNORECASE,
    ))

def execution_lock_store(user_data):
    if not isinstance(user_data, dict):
        return {"fields": {}, "steps": {}}
    store = user_data.setdefault("_execution_locks", {})
    if not isinstance(store, dict):
        store = {}
        user_data["_execution_locks"] = store
    store.setdefault("fields", {})
    store.setdefault("steps", {})
    return store

def page_execution_scope(page):
    host = (urlparse(page.url or "").hostname or "").lower()
    path = (urlparse(page.url or "").path or "").lower()
    try:
        stage = infer_apply_stage(page, [])
    except Exception:
        stage = "unknown"
    return f"{host}:{path}:{stage}"

def field_execution_lock_key(page, field, desired_answer=None):
    raw = "|".join([
        page_execution_scope(page),
        str(field.get("field_id") or ""),
        field_key(field),
        normalized_option_text(desired_answer),
    ])
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]

def current_discovery_field_value(page, field):
    scope = get_scope_by_index(page, field.get("scope_index"))
    selector = field.get("selector")
    if not selector:
        return ""
    try:
        locator = scope.locator(selector).first
        if locator.count() == 0:
            return ""
        input_type = field.get("input_type")
        if input_type in {"checkbox", "radio"}:
            return "checked" if locator.is_checked(timeout=300) else ""
        try:
            value = locator.input_value(timeout=300)
            if value:
                return value
        except Exception:
            pass
        return locator_label(locator)
    except Exception:
        return ""

def discovery_field_matches_answer(page, field, desired_answer=None):
    input_type = field.get("input_type")
    if input_type in {"checkbox", "radio"}:
        if not discovery_field_is_checked(page, field):
            return False
        if desired_answer:
            return answer_matches_field_option(field, desired_answer)
        return True
    current = current_discovery_field_value(page, field)
    if not current:
        return False
    if not desired_answer:
        return True
    current_norm = normalized_option_text(current)
    desired_norm = normalized_option_text(normalize_discovery_value(field.get("label"), input_type, desired_answer))
    return bool(desired_norm and (current_norm == desired_norm or desired_norm in current_norm or current_norm in desired_norm))

def execution_field_locked_valid(page, field, desired_answer, user_data):
    store = execution_lock_store(user_data)
    lock_key = field_execution_lock_key(page, field, desired_answer)
    lock = store.get("fields", {}).get(lock_key)
    if not lock or lock.get("status") != "valid":
        return False
    return discovery_field_matches_answer(page, field, desired_answer)

def mark_execution_field_valid(page, field, desired_answer, user_data, source):
    store = execution_lock_store(user_data)
    lock_key = field_execution_lock_key(page, field, desired_answer)
    store.setdefault("fields", {})[lock_key] = {
        "status": "valid",
        "field": field_key(field)[:180],
        "value": str(desired_answer or "")[:180],
        "source": source,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    return lock_key

def field_requires_user(field, user_data, req, allow_placeholders=None):
    if not field.get("required") or field.get("value_present"):
        return False
    if field.get("input_type") in {"hidden", "submit", "button"}:
        return False
    if field_answer_override(field.get("label"), user_data):
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

def field_can_defer_to_question_probe(field, req):
    if not getattr(req, "probe_fill_unapproved_questions", True):
        return False
    input_type = field.get("input_type")
    if input_type in {"input", "search"}:
        input_type = "text"
    if input_type not in PROBE_SUPPORTED_CONTROL_TYPES:
        return False
    if is_protected_workday_country_field(field):
        return False
    text = semantic_field_text(field)
    if input_type == "checkbox" and not is_safe_discovery_checkbox(field):
        return bool(PROBE_IMMEDIATE_STOP_RE.search(text))
    return True

def is_safe_discovery_checkbox(field):
    if field.get("input_type") != "checkbox":
        return False
    return bool(SAFE_DISCOVERY_CHECKBOX_RE.search(field_key(field)))

def is_self_identification_decline_field(field):
    if field.get("input_type") not in {"checkbox", "radio"}:
        return False
    text = " ".join(str(field.get(key, "")) for key in ["label", "raw_label", "name", "id", "canonical_field"]).lower()
    if not re.search(r"(disability|veteran|gender|ethnicity|race|self.identif|selfidentif)", text, re.IGNORECASE):
        return False
    return bool(re.search(
        r"(do\s+not\s+want\s+to\s+answer|do\s+not\s+wish\s+to\s+answer|don't\s+want\s+to\s+answer|don't\s+wish\s+to\s+answer|prefer\s+not\s+to\s+answer|decline\s+to\s+self)",
        text,
        re.IGNORECASE,
    ))

def self_identification_group_token(field):
    text = " ".join(str(field.get(key, "")) for key in ["name", "id", "canonical_field", "label"]).lower()
    for token in ["disabilitystatus", "disability", "veteran", "ethnicity", "gender", "race"]:
        if token in normalized_option_text(text).replace(" ", ""):
            return token
    return ""

def discovery_field_is_checked(page, field):
    if field.get("input_type") not in {"checkbox", "radio"}:
        return False
    scope = get_scope_by_index(page, field.get("scope_index"))
    selector = field.get("selector")
    if not selector:
        return False
    try:
        locator = scope.locator(selector).first
        return bool(locator.count() and locator.is_checked(timeout=300))
    except Exception:
        return False

def self_identification_decline_selected(page, field):
    token = self_identification_group_token(field)
    if not token:
        return False
    scope = get_scope_by_index(page, field.get("scope_index"))
    try:
        return bool(scope.evaluate(
            """
            (token) => {
              const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
              const norm = value => clean(value).toLowerCase().replace(/[^a-z0-9]+/g, "");
              const tokenNorm = norm(token);
              const decline = text => /(do not want to answer|do not wish to answer|don't want to answer|don't wish to answer|prefer not to answer|decline to self)/i.test(text);
              const checked = Array.from(document.querySelectorAll('input[type="checkbox"]:checked, input[type="radio"]:checked'));
              for (const input of checked) {
                const key = norm(`${input.id || ""} ${input.name || ""}`);
                if (tokenNorm && !key.includes(tokenNorm) && !(tokenNorm === "disability" && key.includes("disabilitystatus"))) continue;
                const labels = input.labels ? Array.from(input.labels).map(label => clean(label.innerText || label.textContent || "")) : [];
                const siblingText = clean(input.parentElement && (input.parentElement.innerText || input.parentElement.textContent || ""));
                if (labels.some(decline) || decline(siblingText)) return true;
              }
              return false;
            }
            """,
            token,
        ))
    except Exception:
        return False

def get_scope_by_index(page, scope_index):
    scopes = get_apply_scopes(page)
    if scope_index is None:
        return scopes[0]
    try:
        return scopes[int(scope_index)]
    except Exception:
        return scopes[0]

def radio_group_key(field):
    if field.get("input_type") != "radio":
        return None
    name = str(field.get("name") or "").strip()
    if name:
        return f"{field.get('scope_index', 0)}:{name}"
    label = normalized_option_text(field.get("label"))
    label = re.sub(r"\b(yes|no|true|false|decline|accept|prefer not to answer|i do not wish to answer)\b$", "", label).strip()
    return f"{field.get('scope_index', 0)}:{label}"

def radio_group_has_checked(page, field):
    if field.get("input_type") != "radio":
        return False
    scope = get_scope_by_index(page, field.get("scope_index"))
    name = str(field.get("name") or "").strip()
    if name:
        try:
            escaped = quoted_css_attr(name)
            return scope.locator(f'input[type="radio"][name="{escaped}"]:checked').count() > 0
        except Exception:
            pass
    selector = field.get("selector")
    if selector:
        try:
            return scope.locator(selector).first.is_checked(timeout=300)
        except Exception:
            return False
    return False

def normalized_option_text(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

US_COUNTRY_NORMALIZED = {
    "united states of america",
    "united states",
    "usa",
    "us",
    "u s",
    "u s a",
}
PROTECTED_COUNTRY_PLACEHOLDERS = {
    "",
    "select",
    "select one",
    "choose",
    "choose an answer",
    "choose an option",
    normalized_option_text(PROBE_TEXT_PLACEHOLDER),
}

def is_us_country_value(value):
    return normalized_option_text(value) in US_COUNTRY_NORMALIZED

def profile_country_value(user_data):
    raw = (user_data or {}).get("country") or "United States"
    return "United States of America" if is_us_country_value(raw) else str(raw).strip()

def is_protected_workday_country_field(field):
    label_norm = normalized_option_text(" ".join(str(field.get(key, "") or "") for key in ["label", "raw_label"]))
    text = " ".join(str(field.get(key, "") or "") for key in [
        "label", "raw_label", "name", "id", "placeholder", "canonical_field", "selector"
    ])
    norm = normalized_option_text(text)
    compact = norm.replace(" ", "")
    if not norm:
        return False
    if label_norm in {"region", "state", "province", "state or territory"}:
        return False
    if "addresscountryregion" in compact and "country" not in label_norm:
        return False
    if re.search(r"\b(phone|calling|dial|device|citizenship|nationality)\b", norm):
        return False
    if "countryphone" in compact or "phonecountry" in compact or "countrycalling" in compact:
        return False
    if field.get("canonical_field") == "country":
        return True
    return bool(
        "country region" in norm
        or "residence country" in norm
        or "address country" in norm
        or re.search(r"(^|\s)country($|\s)", norm)
    )

def protected_country_visible_value(page, field):
    selector = field.get("selector")
    if selector:
        try:
            scope = get_scope_by_index(page, field.get("scope_index"))
            locator = scope.locator(selector).first
            if locator.count():
                text = workday_country_display_text(locator) or workday_own_control_text(locator)
                if text:
                    return text
        except Exception:
            pass
    return str(field.get("value") or "").strip()

def protected_country_is_blank_or_placeholder(value):
    return normalized_option_text(value) in PROTECTED_COUNTRY_PLACEHOLDERS

def fill_protected_workday_country_field(page, field, user_data):
    expected = profile_country_value(user_data)
    actual_before = protected_country_visible_value(page, field)
    if is_us_country_value(actual_before):
        return {
            "status": "ok",
            "changed": False,
            "expected_country": "United States of America" if is_us_country_value(expected) else expected,
            "actual_country": actual_before,
        }
    if not expected:
        return {
            "status": "needs_technical_review",
            "reason": "protected_country_missing_profile_country",
            "expected_country": "United States of America",
            "actual_country": actual_before,
        }
    fill_value = "United States of America" if is_us_country_value(expected) else expected
    changed = fill_discovery_field(page, field, fill_value, allow_confirmed_sensitive=True)
    page.wait_for_timeout(700)
    actual_after = protected_country_visible_value(page, field)
    if is_us_country_value(fill_value) and is_us_country_value(actual_after):
        return {
            "status": "ok",
            "changed": bool(changed),
            "expected_country": "United States of America",
            "actual_country": actual_after,
        }
    return {
        "status": "needs_technical_review",
        "changed": bool(changed),
        "reason": "protected_country_mismatch",
        "expected_country": "United States of America" if is_us_country_value(fill_value) else fill_value,
        "actual_country": actual_after or actual_before or "",
    }

def quoted_css_attr(value):
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')

def discovery_option_terms(label, value):
    terms = {normalized_option_text(value)}
    label_low = (label or "").lower()
    value_norm = normalized_option_text(value)

    if re.search(r"\b(overall|proficiency|language level|level)\b", label_low):
        if value_norm in {"4 fluent", "fluent", "professional working proficiency", "professional proficiency"}:
            terms.update({
                "4 fluent",
                "4 - fluent",
                "fluent",
            })
        if value_norm in {"5 native", "native", "native bilingual proficiency", "bilingual"}:
            terms.update({
                "5 native",
                "5 - native",
                "native",
                "native bilingual",
            })
        if value_norm in {"3 advanced", "advanced", "limited working proficiency"}:
            terms.update({
                "3 advanced",
                "3 - advanced",
                "advanced",
            })

    if value_norm in {"bachelor s degree", "bachelors degree", "bachelor degree", "bachelor"}:
        terms.update({
            "bachelor degree",
            "bachelor s degree",
            "bachelors degree",
            "undergraduate degree",
        })

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

def checkbox_answer_state(value):
    norm = normalized_option_text(value)
    if norm in {"true", "yes", "y", "1", "checked", "check", "agree", "accept", "acknowledge", "i agree"}:
        return True
    if norm in {"false", "no", "n", "0", "unchecked", "do not agree", "decline", "none"}:
        return False
    return None

def radio_yes_no_option_matches(field, value):
    desired = normalized_option_text(value)
    if desired not in {"yes", "no"}:
        return None
    option_identity = " ".join([
        normalized_option_text(field.get("value")),
        normalized_option_text(field.get("id")),
    ])
    option_tokens = option_identity.split()
    for token in reversed(option_tokens):
        if token in {"yes", "no"}:
            return token == desired
    combined = " ".join([
        normalized_option_text(field.get("label")),
        normalized_option_text(field.get("raw_label")),
        normalized_option_text(field.get("name")),
    ])
    tokens = combined.split()
    yes_no_tokens = [token for token in tokens if token in {"yes", "no"}]
    if not yes_no_tokens:
        return None
    return yes_no_tokens[-1] == desired

def answer_matches_field_option(field, value):
    if field.get("input_type") == "radio":
        yes_no_match = radio_yes_no_option_matches(field, value)
        if yes_no_match is not None:
            return yes_no_match
    terms = discovery_option_terms(field.get("label"), value)
    option_texts = {
        normalized_option_text(field.get("label")),
        normalized_option_text(field.get("raw_label")),
        normalized_option_text(field.get("value")),
        normalized_option_text(field.get("name")),
        normalized_option_text(field.get("id")),
    }
    option_texts = {text for text in option_texts if text}
    for term in terms:
        if not term:
            continue
        for option_text in option_texts:
            if term == option_text or option_text == term:
                return True
            if len(term) <= 3:
                if re.search(rf"(^|\s){re.escape(term)}($|\s)", option_text):
                    return True
            elif term in option_text or option_text in term:
                return True
    return False

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
    if re.search(r"(_slt_|visible-input-|combobox|autocomplete)", text, re.IGNORECASE):
        return True
    label_low = (field.get("label") or "").lower()
    if field.get("tag") == "input" and any(token in label_low for token in [
        "how did you hear",
        "source",
        "state",
        "province",
        "phone device type",
        "device type",
        "country phone code",
    ]):
        return True
    return False

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

def fill_discovery_field(page, field, value, allow_confirmed_sensitive=False):
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
        if input_type == "select":
            if select_backing_hidden_select(scope, field, value):
                return True
            try:
                if workday_locator_value_matches(locator, field.get("label"), value):
                    return True
                locator.scroll_into_view_if_needed(timeout=1000)
                locator.click(timeout=2500, force=True)
                page.wait_for_timeout(600)
                if click_workday_option(page, list(discovery_option_terms(field.get("label"), value))):
                    page.wait_for_timeout(800)
                    return workday_locator_value_matches(locator, field.get("label"), value)
            except Exception:
                pass
            return False
        if input_type == "checkbox":
            if not is_safe_discovery_checkbox(field) and not allow_confirmed_sensitive:
                return False
            desired_state = checkbox_answer_state(value)
            if desired_state is False:
                try:
                    if locator.is_checked(timeout=500):
                        locator.uncheck(timeout=2000, force=True)
                    return True
                except Exception:
                    return True
            if desired_state is None and not is_safe_discovery_checkbox(field) and not answer_matches_field_option(field, value):
                return False
            try:
                if not locator.is_checked(timeout=500):
                    locator.check(timeout=2000, force=True)
                return True
            except Exception:
                locator.click(timeout=2000, force=True)
                return True
        if input_type == "radio":
            if not allow_confirmed_sensitive or not answer_matches_field_option(field, value):
                return False
            try:
                locator.check(timeout=2000, force=True)
                return True
            except Exception:
                locator.click(timeout=2000, force=True)
                return True
        if select_backing_hidden_select(scope, field, value):
            return True
        if is_autocomplete_select_field(field) and fill_autocomplete_select_field(page, scope, locator, field, value):
            return True
        locator.fill(value, timeout=3000)
        return True
    except Exception as err:
        print(f"[Discovery] Could not fill field '{field.get('label')}': {err}")
        return False

def clear_discovery_field(page, field):
    scope = get_scope_by_index(page, field.get("scope_index"))
    selector = field.get("selector")
    if not selector:
        return False
    try:
        locator = scope.locator(selector).first
        if locator.count() == 0 or not locator.is_visible(timeout=500):
            return False
        locator.fill("", timeout=1500)
        return True
    except Exception:
        return False

def fill_labeled_text_control(page, label_pattern, value):
    if not value:
        return False
    regex = re.compile(label_pattern, re.IGNORECASE)
    for scope in get_apply_scopes(page):
        candidates = []
        try:
            candidates.append(scope.get_by_label(regex).first)
        except Exception:
            pass
        try:
            candidates.append(scope.locator(
                f"xpath=//label[contains(normalize-space(.), \"{label_pattern}\")]/following::input[1]"
            ).first)
        except Exception:
            pass
        for locator in candidates:
            try:
                if locator.count() == 0 or not locator.is_visible(timeout=500):
                    continue
                locator.fill(str(value), timeout=2000)
                return True
            except Exception:
                continue
    return False

def clear_labeled_text_control(page, label_pattern):
    regex = re.compile(label_pattern, re.IGNORECASE)
    for scope in get_apply_scopes(page):
        try:
            locator = scope.get_by_label(regex).first
            if locator.count() > 0 and locator.is_visible(timeout=500):
                locator.fill("", timeout=1500)
                return True
        except Exception:
            continue
    return False

def click_workday_option(page, option_terms, max_options=80, visible_timeout=300):
    patterns = [re.compile(term, re.IGNORECASE) for term in option_terms if term]
    normalized_terms = [
        (
            normalized_option_text(re.sub(r"^\^|\$$", "", str(term or ""))),
            str(term or "").startswith("^") and str(term or "").endswith("$"),
        )
        for term in option_terms
        if normalized_option_text(re.sub(r"^\^|\$$", "", str(term or "")))
    ]
    selectors = [
        '[role="option"]',
        '[role="listbox"] [role="option"]',
        '[role="listbox"] li',
        '[data-automation-id*="promptOption" i]',
        '[data-automation-id*="menuItem" i]',
        '[data-automation-label]',
        '[aria-label]',
        'li',
    ]
    for selector in selectors:
        try:
            options = page.locator(selector)
            for index in range(min(options.count(), max_options)):
                option = options.nth(index)
                if not option.is_visible(timeout=visible_timeout):
                    continue
                text = locator_label(option)
                automation = ""
                try:
                    automation = option.get_attribute("data-automation-id") or ""
                except Exception:
                    automation = ""
                if "selecteditem" in automation.lower() or "press delete to clear value" in (text or "").lower():
                    continue
                text_norm = normalized_option_text(text)
                raw_match = text and any(pattern.search(text) for pattern in patterns)
                norm_match = bool(
                    text_norm
                    and any(
                        term == text_norm
                        or (not anchored and len(term) > 2 and (term in text_norm or text_norm in term))
                        for term, anchored in normalized_terms
                    )
                )
                if raw_match or norm_match:
                    option.scroll_into_view_if_needed(timeout=1000)
                    option.click(timeout=2000)
                    page.wait_for_timeout(500)
                    try:
                        page.keyboard.press("Enter", timeout=500)
                    except Exception:
                        pass
                    return True
        except Exception:
            continue
    return False

def click_first_valid_workday_option(page):
    try:
        return page.evaluate("""
            () => {
              const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
              const visible = el => {
                if (!el || !el.isConnected) return false;
                const style = window.getComputedStyle(el);
                const box = el.getBoundingClientRect();
                return !!(box.width && box.height) && style.visibility !== "hidden" && style.display !== "none";
              };
              const placeholder = text => /^(select|select one|choose|choose an answer|choose an option|search)$/i.test(clean(text));
              const bad = text => /\\b(save|continue|submit|review|back|cancel|delete|withdraw)\\b/i.test(text);
              const selectors = [
                '[role="listbox"] [role="option"]',
                '[role="option"]',
                '[data-automation-id*="promptOption" i]',
                '[data-automation-id*="menuItem" i]',
                '[role="listbox"] li',
                'li'
              ];
              const seen = new Set();
              const candidates = [];
              const optionTexts = [];
              for (const selector of selectors) {
                for (const node of Array.from(document.querySelectorAll(selector))) {
                  if (!visible(node) || seen.has(node)) continue;
                  seen.add(node);
                  const automation = clean(node.getAttribute('data-automation-id'));
                  const text = clean(node.innerText || node.textContent || node.getAttribute('aria-label') || node.getAttribute('data-automation-label') || '');
                  if (!text || placeholder(text) || bad(text)) continue;
                  if (/selecteditem/i.test(automation) || /press delete to clear value/i.test(text)) continue;
                  const box = node.getBoundingClientRect();
                  candidates.push({ node, text, top: box.top, left: box.left });
                  if (!optionTexts.includes(text)) optionTexts.push(text);
                }
              }
              candidates.sort((a, b) => a.top - b.top || a.left - b.left);
              const target = candidates[0];
              if (!target) return { clicked: false, reason: "no_valid_option" };
              target.node.scrollIntoView({ block: "nearest", inline: "nearest" });
              target.node.click();
              return { clicked: true, text: target.text, options: optionTexts.slice(0, 40) };
            }
        """) or {"clicked": False, "reason": "empty_result"}
    except Exception as err:
        return {"clicked": False, "reason": str(err)}

def click_first_valid_workday_option_near_control(locator):
    try:
        return locator.evaluate("""
            (el) => {
              const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
              const visible = node => {
                if (!node || !node.isConnected) return false;
                const style = window.getComputedStyle(node);
                const box = node.getBoundingClientRect();
                return !!(box.width && box.height) && style.visibility !== "hidden" && style.display !== "none";
              };
              const placeholder = text => /^(select|select one|choose|choose an answer|choose an option|search)$/i.test(clean(text));
              const bad = text => /\\b(save|continue|submit|review|back|cancel|delete|withdraw)\\b/i.test(text)
                || /\\b(completed|current)?\\s*step\\s+\\d+\\s+of\\s+\\d+\\b/i.test(text);
              const controlBox = el.getBoundingClientRect();
              const overlapsControl = box =>
                box.right >= controlBox.left - 50 &&
                box.left <= controlBox.right + 50 &&
                box.bottom >= controlBox.bottom - 20 &&
                box.top <= controlBox.bottom + 720;
              const selectors = [
                '[role="listbox"] [role="option"]',
                '[role="option"]',
                '[data-automation-id*="promptOption" i]',
                '[data-automation-id*="menuItem" i]',
                '[role="listbox"] li',
                'li'
              ];
              const seen = new Set();
              const candidates = [];
              const optionTexts = [];
              for (const selector of selectors) {
                for (const node of Array.from(document.querySelectorAll(selector))) {
                  if (!visible(node) || seen.has(node)) continue;
                  seen.add(node);
                  if (node.closest('header, nav, [role="navigation"], [aria-label*="Progress" i]')) continue;
                  const automation = clean(node.getAttribute('data-automation-id'));
                  const text = clean(node.innerText || node.textContent || node.getAttribute('aria-label') || node.getAttribute('data-automation-label') || '');
                  if (!text || placeholder(text) || bad(text)) continue;
                  if (/selecteditem/i.test(automation) || /press delete to clear value/i.test(text)) continue;
                  const box = node.getBoundingClientRect();
                  if (!overlapsControl(box)) continue;
                  candidates.push({ node, text, top: box.top, left: box.left });
                  if (!optionTexts.includes(text)) optionTexts.push(text);
                }
              }
              candidates.sort((a, b) => a.top - b.top || a.left - b.left);
              const target = candidates[0];
              if (!target) return { clicked: false, reason: "no_valid_option_near_control", options: optionTexts.slice(0, 40) };
              target.node.scrollIntoView({ block: "nearest", inline: "nearest" });
              target.node.click();
              return { clicked: true, text: target.text, options: optionTexts.slice(0, 40) };
            }
        """) or {"clicked": False, "reason": "empty_result"}
    except Exception as err:
        return {"clicked": False, "reason": str(err)}

def select_first_valid_option_for_field(page, field):
    scope = get_scope_by_index(page, field.get("scope_index"))
    selector = field.get("selector")
    if not selector:
        return None
    try:
        locator = scope.locator(selector).first
        if locator.count() == 0 or not locator.is_visible(timeout=500):
            return None
        tag = (field.get("tag") or "").lower()
        if tag == "select":
            choice = locator.evaluate("""
                el => {
                  const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
                  const placeholder = text => /^(select|select one|choose|choose an answer|choose an option|search)$/i.test(clean(text));
                  const options = Array.from(el.options || [])
                    .map(option => ({ value: option.value, text: clean(option.innerText || option.textContent), disabled: option.disabled }))
                    .filter(option => !option.disabled && option.value && option.text && !placeholder(option.text));
                  const choice = options[0] || null;
                  return choice ? { ...choice, options: options.map(option => option.text).slice(0, 40) } : null;
                }
            """)
            if not choice:
                return None
            try:
                locator.select_option(value=choice["value"], timeout=2000, force=True)
            except Exception:
                locator.evaluate("""
                    (el, value) => {
                      el.value = value;
                      el.dispatchEvent(new Event("input", { bubbles: true }));
                      el.dispatchEvent(new Event("change", { bubbles: true }));
                    }
                """, choice["value"])
            page.wait_for_timeout(500)
            return {"selected": choice.get("text") or choice.get("value"), "options": choice.get("options") or []}
        locator.scroll_into_view_if_needed(timeout=1000)
        clear_workday_selection_near(locator)
        page.wait_for_timeout(250)
        locator.click(timeout=2500, force=True)
        page.wait_for_timeout(600)
        result = click_first_valid_workday_option_near_control(locator)
        if result.get("clicked"):
            page.wait_for_timeout(800)
            return {"selected": result.get("text") or True, "options": result.get("options") or []}
    except Exception as err:
        print(f"[Discovery] Could not select first valid option for '{field.get('label')}': {err}")
    return None

def can_choose_first_valid_required_select(field):
    if not field.get("required") or field.get("risk") != "low":
        return False
    if field.get("input_type") != "select" or field.get("value_present"):
        return False
    if is_protected_workday_country_field(field):
        return False
    text = semantic_field_text(field).lower()
    if re.search(r"\b(country|country region|region|state|province|phone code|language|overall)\b", text):
        return False
    canonical_key = canonical_key_for_question(text, {"nearest_group_text": field.get("group_text") or ""})
    if canonical_key in {
        "authorized_to_work_us",
        "need_sponsorship",
        "conflict_of_interest",
        "export_control",
        "current_or_previous_company_employee",
    }:
        return False
    if PROBE_IMMEDIATE_STOP_RE.search(text) or HIGH_RISK_FIELD_RE.search(text) or is_business_conflict_disclosure_question(text):
        return False
    if semantic_field_is_sensitive_or_compliance(field):
        return False
    if re.search(r"(gender|race|ethnicity|hispanic|veteran|disability|voluntary self|self-identification|self identification|eeo|equal opportunity|certify|signature|attest)", text):
        return False
    return True

def provisional_select_question_from_field(field, selected_result):
    if not DetectedQuestion or not fingerprint_question or not normalize_question_text:
        return None, {}
    selected_text = selected_result.get("selected") if isinstance(selected_result, dict) else selected_result
    selected_text = str(selected_text or "").strip()
    options = selected_result.get("options") if isinstance(selected_result, dict) else []
    options = [str(option or "").strip() for option in (options or []) if str(option or "").strip()]
    if selected_text and selected_text not in options:
        options.insert(0, selected_text)
    raw_text = ""
    try:
        raw_text = workday_schema_question_text(field)
    except Exception:
        raw_text = ""
    raw_text = raw_text or clean_question_candidate(field.get("group_text")) or field_key(field) or field.get("label") or "Required select question"
    normalized_text = normalize_question_text(raw_text)
    fingerprint = fingerprint_question(normalized_text, "select", options)
    context = {
        "nearest_group_text": field.get("group_text") or "",
        "label_for_text": field.get("raw_label") or field.get("label") or "",
        "validation_message": field.get("validation_message") or "",
        "options": options,
    }
    question = DetectedQuestion(
        raw_text=raw_text,
        normalized_text=normalized_text,
        fingerprint=fingerprint,
        required=True,
        control_type="select",
        options=options,
        validation_message="",
        locator_hints={
            "tag": field.get("tag"),
            "type": field.get("input_type"),
            "id": field.get("id"),
            "name": field.get("name"),
            "data_field": field.get("data_field"),
            "data_question": field.get("data_question"),
        },
        status=UNANSWERED,
        canonical_key=canonical_key_for_question(raw_text, context),
        question_context=context,
    )
    metadata = {
        "probe_answer_applied": True,
        "probe_answer": selected_text,
        "requires_user_review": True,
        "probe_control_type": "select",
        "conditional_branch_probe": True,
        "branch_probe_answer": selected_text,
        "branch_probe_warning": PROBE_BRANCH_WARNING,
        "first_valid_option_probe": True,
    }
    return question, metadata

def scroll_visible_workday_option_list(page, amount=650):
    try:
        return bool(page.evaluate("""
            (amount) => {
              const visible = el => {
                if (!el || !el.isConnected) return false;
                const style = window.getComputedStyle(el);
                const box = el.getBoundingClientRect();
                return !!(box.width && box.height) && style.visibility !== "hidden" && style.display !== "none";
              };
              const candidates = Array.from(document.querySelectorAll('[role="listbox"], ul, div'))
                .filter(visible)
                .filter(el => el.scrollHeight > el.clientHeight + 20)
                .map(el => {
                  const text = String(el.innerText || el.textContent || "");
                  const optionCount = el.querySelectorAll('[role="option"], li').length;
                  return { el, text, optionCount, area: el.clientWidth * el.clientHeight };
                })
                .filter(item => item.optionCount || /Australia|Uganda|United|States|Country|Select One/i.test(item.text))
                .sort((a, b) => b.optionCount - a.optionCount || b.area - a.area);
              const target = candidates[0] && candidates[0].el;
              if (!target) return false;
              target.scrollTop = target.scrollTop + amount;
              target.dispatchEvent(new Event('scroll', { bubbles: true }));
              return true;
            }
        """, amount))
    except Exception:
        return False

def click_workday_dropdown_option_near_control(locator, exact_texts):
    exact_norms = [
        normalized_option_text(text)
        for text in exact_texts
        if normalized_option_text(text)
    ]
    if not exact_norms:
        return {"clicked": False, "reason": "missing_exact_texts"}
    try:
        return locator.evaluate("""
            (el, exactNorms) => {
              const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
              const norm = value => clean(value).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
              const visible = node => {
                if (!node || !node.isConnected) return false;
                const style = window.getComputedStyle(node);
                const box = node.getBoundingClientRect();
                return !!(box.width && box.height) && style.visibility !== "hidden" && style.display !== "none";
              };
              const buttonBox = el.getBoundingClientRect();
              const overlapsControl = box =>
                box.right >= buttonBox.left - 40 &&
                box.left <= buttonBox.right + 40 &&
                box.bottom >= buttonBox.bottom - 30 &&
                box.top <= buttonBox.bottom + 720;
              const selectors = [
                '[role="option"]',
                '[data-automation-id*="promptOption" i]',
                '[data-automation-id*="menuItem" i]',
                '[data-automation-label]',
                'li',
                'div',
                'span',
                'button'
              ];
              const candidates = [];
              const seen = new Set();
              for (const selector of selectors) {
                for (const node of Array.from(document.querySelectorAll(selector))) {
                  if (!visible(node) || seen.has(node)) continue;
                  seen.add(node);
                  const text = clean(node.innerText || node.textContent || node.getAttribute('aria-label') || node.getAttribute('data-automation-label') || '');
                  if (!text) continue;
                  const lower = text.toLowerCase();
                  if (lower.includes('press delete to clear value')) continue;
                  if ((node.getAttribute('data-automation-id') || '').toLowerCase().includes('selecteditem')) continue;
                  const textNorm = norm(text);
                  if (!exactNorms.includes(textNorm)) continue;
                  const box = node.getBoundingClientRect();
                  if (!overlapsControl(box)) continue;
                  candidates.push({ node, text, top: box.top, left: box.left });
                }
              }
              candidates.sort((a, b) => a.top - b.top || a.left - b.left);
              const target = candidates[0];
              if (!target) {
                return { clicked: false, reason: "exact_option_not_visible" };
              }
              target.node.scrollIntoView({ block: "nearest", inline: "nearest" });
              target.node.click();
              return { clicked: true, text: target.text };
            }
        """, exact_norms) or {"clicked": False, "reason": "empty_result"}
    except Exception as err:
        return {"clicked": False, "reason": str(err)}

def scroll_workday_dropdown_near_control(locator, amount=520):
    try:
        return locator.evaluate("""
            (el, amount) => {
              const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
              const visible = node => {
                if (!node || !node.isConnected) return false;
                const style = window.getComputedStyle(node);
                const box = node.getBoundingClientRect();
                return !!(box.width && box.height) && style.visibility !== "hidden" && style.display !== "none";
              };
              const buttonBox = el.getBoundingClientRect();
              const overlap = box => Math.max(0, Math.min(box.right, buttonBox.right + 80) - Math.max(box.left, buttonBox.left - 80));
              const candidates = Array.from(document.querySelectorAll('[role="listbox"], ul, div'))
                .filter(visible)
                .filter(node => node.scrollHeight > node.clientHeight + 8)
                .map(node => {
                  const box = node.getBoundingClientRect();
                  const text = clean(node.innerText || node.textContent || "");
                  const countryHints = /Afghanistan|Australia|Anguilla|Norway|Pakistan|United|Uganda|Zimbabwe/i.test(text) ? 1000 : 0;
                  const near = box.top >= buttonBox.bottom - 80 && box.top <= buttonBox.bottom + 260 ? 500 : 0;
                  const horizontal = overlap(box);
                  const optionCount = node.querySelectorAll('[role="option"], li').length;
                  return { node, box, score: countryHints + near + horizontal + optionCount * 3 };
                })
                .filter(item => item.score > 0)
                .sort((a, b) => b.score - a.score);
              const target = candidates[0] && candidates[0].node;
              if (!target) return { scrolled: false, reason: "no_dropdown_container" };
              const before = target.scrollTop;
              target.scrollTop = Math.max(0, Math.min(target.scrollHeight, before + amount));
              target.dispatchEvent(new Event('scroll', { bubbles: true }));
              target.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: amount }));
              return { scrolled: target.scrollTop !== before, before, after: target.scrollTop };
            }
        """, amount) or {"scrolled": False, "reason": "empty_result"}
    except Exception as err:
        return {"scrolled": False, "reason": str(err)}

def select_workday_dropdown_exact_near_control(page, locator, exact_texts, scroll_attempts=40):
    click_result = click_workday_dropdown_option_near_control(locator, exact_texts)
    if click_result.get("clicked"):
        page.wait_for_timeout(700)
        return {"clicked": True, "method": "visible_exact", "result": click_result}
    for _ in range(scroll_attempts):
        scroll_result = scroll_workday_dropdown_near_control(locator, amount=520)
        if not scroll_result.get("scrolled"):
            try:
                box = locator.bounding_box()
                if box:
                    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] + 120)
                page.mouse.wheel(0, 520)
            except Exception:
                pass
        page.wait_for_timeout(180)
        click_result = click_workday_dropdown_option_near_control(locator, exact_texts)
        if click_result.get("clicked"):
            page.wait_for_timeout(700)
            return {"clicked": True, "method": "scroll_exact", "result": click_result, "scroll": scroll_result}
    return {"clicked": False, "method": "not_found", "last_result": click_result}

def click_workday_option_with_scroll(
    page,
    option_terms,
    scroll_attempts=35,
    scroll_amount=650,
    max_options=80,
    visible_timeout=300,
):
    if click_workday_option(page, option_terms, max_options=max_options, visible_timeout=visible_timeout):
        return True
    for _ in range(scroll_attempts):
        if not scroll_visible_workday_option_list(page, amount=scroll_amount):
            try:
                page.mouse.wheel(0, scroll_amount)
            except Exception:
                pass
        page.wait_for_timeout(250)
        if click_workday_option(page, option_terms, max_options=max_options, visible_timeout=visible_timeout):
            return True
    return False

def visible_workday_option_texts(page, limit=30):
    try:
        return page.evaluate("""
            (limit) => {
              const visible = el => {
                if (!el || !el.isConnected) return false;
                const style = window.getComputedStyle(el);
                const box = el.getBoundingClientRect();
                return !!(box.width && box.height) && style.visibility !== "hidden" && style.display !== "none";
              };
              const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
              const selectors = [
                '[role="listbox"] [role="option"]',
                '[role="option"]',
                '[data-automation-id*="promptOption" i]',
                '[data-automation-id*="menuItem" i]',
                '[role="listbox"] li',
                'li'
              ];
              const seen = new Set();
              const out = [];
              for (const selector of selectors) {
                for (const node of Array.from(document.querySelectorAll(selector))) {
                  if (!visible(node)) continue;
                  const text = clean(node.innerText || node.textContent || node.getAttribute('aria-label') || node.getAttribute('data-automation-label') || '');
                  if (!text || seen.has(text)) continue;
                  seen.add(text);
                  out.push(text);
                  if (out.length >= limit) return out;
                }
              }
              return out;
            }
        """, limit) or []
    except Exception:
        return []

def select_visible_workday_option(page, option_terms, target_text, reason, scroll_attempts=24, scroll_amount=650):
    if click_workday_option(page, option_terms):
        return {"acted": True, "method": "dom", "visible_options": visible_workday_option_texts(page, limit=12)}
    last_visual = None
    for _ in range(scroll_attempts):
        visual_result = visual_click_visible_text_option(page, target_text, reason=reason)
        last_visual = visual_result
        if visual_result.get("acted"):
            return {
                "acted": True,
                "method": "vision",
                "visual": visual_result,
                "visible_options": visible_workday_option_texts(page, limit=12),
            }
        if click_workday_option(page, option_terms):
            return {"acted": True, "method": "dom_after_vision", "visible_options": visible_workday_option_texts(page, limit=12)}
        if not scroll_visible_workday_option_list(page, amount=scroll_amount):
            try:
                page.mouse.wheel(0, scroll_amount)
            except Exception:
                pass
        page.wait_for_timeout(300)
    return {
        "acted": False,
        "method": "not_found",
        "visual": last_visual,
        "visible_options": visible_workday_option_texts(page, limit=20),
    }

def visual_click_visible_text_option(page, target_text, reason="select_option"):
    if not client or not target_text:
        return {"acted": False, "reason": "vision_client_unavailable" if not client else "missing_target"}
    screenshot_dir = os.path.join(os.path.dirname(__file__), "..", "screenshots")
    os.makedirs(screenshot_dir, exist_ok=True)
    screenshot_bytes = page.screenshot(type="png", full_page=False)
    img_w, img_h = get_png_dimensions(screenshot_bytes)
    screenshot_path = os.path.join(screenshot_dir, f"workday_option_vision_{int(time.time())}.png")
    try:
        with open(screenshot_path, "wb") as f:
            f.write(screenshot_bytes)
    except Exception:
        screenshot_path = ""
    image_part = types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png")
    prompt = f"""
You are selecting a value from an already-open Workday dropdown/listbox.

Target visible option text: {target_text}
Reason: {reason}
Screenshot resolution: {img_w}x{img_h}.

Rules:
- Find the visible dropdown/listbox option whose text exactly matches or clearly contains the target text.
- Return the center coordinates of that option.
- Do NOT click Save, Continue, Submit, Review, Back, or any navigation control.
- If the target option is not visible in the screenshot, return action "not_visible".

Return ONLY valid JSON:
{{
  "action": "click" | "not_visible",
  "x": integer_or_null,
  "y": integer_or_null,
  "matched_text": "visible option text or empty",
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
    if str(action_json.get("action") or "").lower() != "click":
        return {
            "acted": False,
            "reason": action_json.get("reason") or "target_not_visible",
            "screenshot_path": screenshot_path,
            "matched_text": action_json.get("matched_text"),
        }
    x, y = scale_coordinates(action_json.get("x"), action_json.get("y"), img_w, img_h, page)
    if x is None or y is None:
        return {"acted": False, "reason": "vision_missing_coordinates", "screenshot_path": screenshot_path}
    label = element_label_at_point(page, x, y)
    if FINAL_SUBMIT_RE.search(label or ""):
        return {"acted": False, "reason": "vision_target_final_submit_guard", "screenshot_path": screenshot_path, "element_label": label}
    page.mouse.click(x, y)
    page.wait_for_timeout(1200)
    return {
        "acted": True,
        "reason": action_json.get("reason"),
        "screenshot_path": screenshot_path,
        "matched_text": action_json.get("matched_text"),
        "element_label": label,
    }

def type_into_open_workday_prompt(page, query, allow_keyboard_type=True):
    if not query:
        return False
    try:
        handle = page.evaluate_handle("""
            () => {
              const visible = el => {
                if (!el || !el.isConnected) return false;
                const style = window.getComputedStyle(el);
                const box = el.getBoundingClientRect();
                return !!(box.width && box.height) && style.visibility !== "hidden" && style.display !== "none";
              };
              const editable = el => {
                if (!el || !visible(el)) return false;
                const tag = el.tagName.toLowerCase();
                if (tag === "textarea") return !el.disabled && !el.readOnly;
                if (tag === "input") {
                  const type = (el.getAttribute("type") || "text").toLowerCase();
                  return !["hidden", "checkbox", "radio", "button", "submit", "file", "password"].includes(type) && !el.disabled && !el.readOnly;
                }
                return el.isContentEditable;
              };
              const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
              const active = document.activeElement;
              const popups = Array.from(document.querySelectorAll(
                '[role="dialog"], [role="listbox"], [data-automation-id*="prompt" i], [data-automation-id*="menu" i], [data-automation-id*="popup" i]'
              )).filter(visible);
              if (editable(active)) {
                const activeText = clean(`${active.id || ""} ${active.name || ""} ${active.getAttribute("aria-label") || ""} ${active.getAttribute("placeholder") || ""}`);
                const activeInPopup = popups.some(popup => popup.contains(active));
                if (activeInPopup || /search|prompt|filter/i.test(activeText)) return active;
              }
              const inputs = Array.from(document.querySelectorAll('input, textarea, [contenteditable="true"]'))
                .filter(editable)
                .map(input => {
                  const box = input.getBoundingClientRect();
                  const text = clean(`${input.id || ""} ${input.name || ""} ${input.getAttribute("aria-label") || ""} ${input.getAttribute("placeholder") || ""}`);
                  let popupScore = 0;
                  for (const popup of popups) {
                    if (popup.contains(input)) popupScore += 1000;
                    const pbox = popup.getBoundingClientRect();
                    if (box.top >= pbox.top - 20 && box.bottom <= pbox.bottom + 20 && box.left >= pbox.left - 20 && box.right <= pbox.right + 20) {
                      popupScore += 500;
                    }
                  }
                  if (/search|prompt|filter/i.test(text)) popupScore += 100;
                  return { input, popupScore, top: box.top };
                })
                .filter(item => item.popupScore > 0)
                .sort((a, b) => b.popupScore - a.popupScore || a.top - b.top);
              return inputs.length ? inputs[0].input : null;
            }
        """)
        element = handle.as_element()
        if not element:
            if not allow_keyboard_type:
                return False
            page.keyboard.type(str(query), delay=15)
            page.wait_for_timeout(900)
            return True
        try:
            element.fill(str(query), timeout=2000)
        except Exception:
            element.focus()
            page.keyboard.press("Control+A", timeout=500)
            page.keyboard.type(str(query), delay=15)
        page.wait_for_timeout(900)
        return True
    except Exception:
        return False

def workday_locator_value_matches(locator, label, value):
    terms = discovery_option_terms(label, value)
    if not terms:
        return False
    def norm_matches(text_norm):
        return any(term and (term == text_norm or term in text_norm or text_norm in term) for term in terms)
    try:
        tag_name = (locator.evaluate("el => el.tagName.toLowerCase()") or "").lower()
        if tag_name == "select":
            selected = locator.evaluate("""
                el => {
                  const option = el.options && el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
                  return option ? `${option.value} ${option.innerText}` : el.value;
                }
            """)
            selected_norm = normalized_option_text(selected)
            return norm_matches(selected_norm)
    except Exception:
        pass
    try:
        own_value = locator.evaluate("""
            el => {
              const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
              return [
                clean(el.value),
                clean(el.innerText || el.textContent || ""),
                clean(el.getAttribute("aria-label")),
                clean(el.getAttribute("title"))
              ].filter(Boolean).join(" ");
            }
        """)
        own_norm = normalized_option_text(own_value)
        if own_norm:
            if norm_matches(own_norm):
                return True
            # For Workday prompt buttons/comboboxes, the element text is the selected value.
            # Do not let a parent container that also contains Phone Country Code make
            # Country=Australia look like Country=United States.
            tag_name = (locator.evaluate("el => el.tagName.toLowerCase()") or "").lower()
            role = (locator.get_attribute("role", timeout=300) or "").lower()
            aria_haspopup = (locator.get_attribute("aria-haspopup", timeout=300) or "").lower()
            if tag_name in {"button", "select"} or role == "combobox" or "listbox" in aria_haspopup:
                if not re.search(r"\b(select one|search|choose|open|list|menu)\b", own_norm, re.IGNORECASE):
                    return False
    except Exception:
        pass
    try:
        current = locator.evaluate("""
            el => {
              const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
              const chunks = [];
              chunks.push(clean(el.value));
              chunks.push(clean(el.innerText || el.textContent || ""));
              chunks.push(clean(el.getAttribute("aria-label")));
              let node = el;
              for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {
                const text = clean(node.innerText || node.textContent || "");
                if (text && text.length < 900) chunks.push(text);
              }
              return chunks.join(" ");
            }
        """)
        current_norm = normalized_option_text(current)
        return norm_matches(current_norm)
    except Exception:
        return False

def get_workday_visible_labeled_control(scope, label_patterns, avoid_patterns=None, after_patterns=None, before_patterns=None):
    try:
        handle = scope.evaluate_handle(
            """
            (args) => {
              const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
              const visible = el => {
                if (!el || !el.isConnected) return false;
                const style = window.getComputedStyle(el);
                if (style.visibility === "hidden" || style.display === "none") return false;
                const box = el.getBoundingClientRect();
                return !!(box.width && box.height);
              };
              const textOf = el => clean(
                (el.innerText || el.textContent || el.value || el.getAttribute("aria-label") || "")
              );
              const build = values => (values || []).map(value => {
                try { return new RegExp(value, "i"); } catch { return null; }
              }).filter(Boolean);
              const labelPatterns = build(args.label_patterns);
              const avoidPatterns = build(args.avoid_patterns);
              const afterPatterns = build(args.after_patterns);
              const beforePatterns = build(args.before_patterns);
              const textNodes = Array.from(document.querySelectorAll("label, span, div, p, legend, h1, h2, h3"))
                .filter(visible)
                .map(node => {
                  const box = node.getBoundingClientRect();
                  return { node, text: textOf(node), top: box.top, bottom: box.bottom, left: box.left, right: box.right };
                })
                .filter(item => item.text);

              let startY = -Infinity;
              if (afterPatterns.length) {
                for (const item of textNodes) {
                  if (afterPatterns.some(pattern => pattern.test(item.text))) {
                    startY = Math.max(startY, item.bottom);
                  }
                }
              }
              let endY = Infinity;
              if (beforePatterns.length) {
                for (const item of textNodes) {
                  if (item.top <= startY) continue;
                  if (beforePatterns.some(pattern => pattern.test(item.text))) {
                    endY = Math.min(endY, item.top);
                  }
                }
              }

              const labels = textNodes
                .filter(item => item.top >= startY - 8 && item.top <= endY + 8)
                .filter(item => labelPatterns.some(pattern => pattern.test(item.text)))
                .filter(item => !avoidPatterns.some(pattern => pattern.test(item.text)))
                .sort((a, b) => {
                  const exactA = labelPatterns.some(pattern => pattern.test(a.text)) && a.text.length <= 80 ? 0 : 1;
                  const exactB = labelPatterns.some(pattern => pattern.test(b.text)) && b.text.length <= 80 ? 0 : 1;
                  return exactA - exactB || a.text.length - b.text.length || a.top - b.top;
                });

              const controlSelector = [
                "select",
                "button[aria-haspopup]",
                "button[aria-expanded]",
                "[role='combobox']",
                "input[role='combobox']",
                "input[id*='--']",
                "button",
                "div[tabindex]"
              ].join(",");
              const badControl = text => /\b(save|continue|submit|review|back|cancel|delete|withdraw|linkedin)\b/i.test(text);

              for (const item of labels.slice(0, 10)) {
                const labelBox = item.node.getBoundingClientRect();
                let root = item.node;
                for (let depth = 0; root && depth < 7; depth++, root = root.parentElement) {
                  const controls = Array.from(root.querySelectorAll(controlSelector))
                    .filter(visible)
                    .filter(control => control !== item.node && !item.node.contains(control))
                    .map(control => {
                      const box = control.getBoundingClientRect();
                      const text = textOf(control);
                      const role = control.getAttribute("role") || "";
                      const aria = control.getAttribute("aria-haspopup") || control.getAttribute("aria-expanded") || "";
                      const tag = control.tagName.toLowerCase();
                      return { control, box, text, role, aria, tag };
                    })
                    .filter(entry => {
                      if (badControl(entry.text)) return false;
                      if (entry.box.top < labelBox.top - 16) return false;
                      if (entry.box.top - labelBox.top > 170) return false;
                      const aligned = Math.abs(entry.box.left - labelBox.left) < 520 || Math.abs(entry.box.right - labelBox.right) < 520;
                      if (!aligned) return false;
                      return true;
                    })
                    .sort((a, b) => {
                      const score = entry => {
                        let value = 0;
                        if (entry.tag === "select") value -= 40;
                        if (/combobox/i.test(entry.role) || /listbox/i.test(entry.aria)) value -= 30;
                        if (/select one|australia|united states|illinois|mobile/i.test(entry.text)) value -= 15;
                        value += Math.abs(entry.box.top - labelBox.bottom);
                        return value;
                      };
                      return score(a) - score(b);
                    });
                  if (controls.length) return controls[0].control;
                }
              }
              return null;
            }
            """,
            {
                "label_patterns": label_patterns,
                "avoid_patterns": avoid_patterns or [],
                "after_patterns": after_patterns or [],
                "before_patterns": before_patterns or [],
            }
        )
        element = handle.as_element()
        return element
    except Exception:
        return None

def choose_workday_visible_labeled_option(page, label_patterns, value, avoid_patterns=None, after_patterns=None, before_patterns=None):
    if not value:
        return False
    terms = list(discovery_option_terms(" ".join(label_patterns), value))
    if normalized_option_text(value) == "mobile":
        terms.extend(["Mobile", "Cell", "Cellular"])
    value_norm = normalized_option_text(value)
    if value_norm in US_STATE_ALIASES:
        terms.append(US_STATE_ALIASES[value_norm])
    for scope in get_apply_scopes(page):
        try:
            element = get_workday_visible_labeled_control(
                scope,
                label_patterns,
                avoid_patterns=avoid_patterns,
                after_patterns=after_patterns,
                before_patterns=before_patterns,
            )
            if not element:
                continue
            if workday_locator_value_matches(element, " ".join(label_patterns), value):
                return True
            try:
                tag_name = (element.evaluate("el => el.tagName.toLowerCase()") or "").lower()
                if tag_name == "select":
                    options = element.evaluate("""
                        el => Array.from(el.options || [])
                          .map(option => ({ value: option.value, text: option.innerText.trim(), disabled: option.disabled }))
                    """)
                    choice = choose_matching_option(options, " ".join(label_patterns), value)
                    if choice:
                        element.select_option(value=choice["value"], timeout=2000, force=True)
                        page.wait_for_timeout(900)
                        return workday_locator_value_matches(element, " ".join(label_patterns), value)
            except Exception:
                pass
            element.scroll_into_view_if_needed(timeout=1000)
            clear_workday_selection_near(element)
            page.wait_for_timeout(250)
            element.click(timeout=2500, force=True)
            page.wait_for_timeout(600)
            try:
                element.fill(str(value), timeout=1000)
                page.wait_for_timeout(500)
            except Exception:
                try:
                    page.keyboard.type(str(value), delay=15)
                    page.wait_for_timeout(500)
                except Exception:
                    pass
            if click_workday_option(page, terms):
                page.wait_for_timeout(900)
                if workday_locator_value_matches(element, " ".join(label_patterns), value):
                    return True
            try:
                element.click(timeout=1500, force=True)
                page.keyboard.press("Control+A", timeout=500)
                page.keyboard.type(str(value), delay=15)
                page.wait_for_timeout(400)
                page.keyboard.press("Enter", timeout=1000)
                page.wait_for_timeout(900)
                if workday_locator_value_matches(element, " ".join(label_patterns), value):
                    return True
            except Exception:
                pass
        except Exception:
            continue
    return False

def choose_workday_control_by_selector(page, selector, value, label=""):
    if not selector or not value:
        return False
    terms = list(discovery_option_terms(label or selector, value))
    value_norm = normalized_option_text(value)
    if value_norm in US_STATE_ALIASES:
        terms.append(US_STATE_ALIASES[value_norm])
    for scope in get_apply_scopes(page):
        try:
            locator = scope.locator(selector).first
            if locator.count() == 0 or not locator.is_visible(timeout=700):
                continue
            if workday_locator_value_matches(locator, label or selector, value):
                return True
            locator.scroll_into_view_if_needed(timeout=1000)
            locator.click(timeout=2500, force=True)
            page.wait_for_timeout(700)
            if click_workday_option(page, terms):
                page.wait_for_timeout(1200)
                if workday_locator_value_matches(locator, label or selector, value):
                    return True
            try:
                locator.click(timeout=1500, force=True)
                page.wait_for_timeout(300)
                page.keyboard.type(str(value), delay=20)
                page.wait_for_timeout(700)
                if click_workday_option(page, terms):
                    page.wait_for_timeout(1000)
                    if workday_locator_value_matches(locator, label or selector, value):
                        return True
                page.keyboard.press("Enter", timeout=1000)
                page.wait_for_timeout(1000)
                if workday_locator_value_matches(locator, label or selector, value):
                    return True
            except Exception:
                pass
        except Exception:
            continue
    return False

def workday_own_control_text(locator):
    try:
        return compact_text(locator.evaluate("""
            el => {
              const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
              return [
                clean(el.value),
                clean(el.innerText || el.textContent || ""),
                clean(el.getAttribute("aria-label")),
                clean(el.getAttribute("title"))
              ].filter(Boolean).join(" ");
            }
        """))
    except Exception:
        return ""

def workday_country_display_text(locator):
    try:
        return compact_text(locator.evaluate("""
            el => {
              const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
              const own = clean(el.innerText || el.textContent || "");
              if (own) return own;
              const aria = clean(el.getAttribute("aria-label"));
              if (aria) {
                return aria.replace(/^Country\\s+/i, "").replace(/\\s+Required$/i, "").trim();
              }
              return clean(el.value || el.getAttribute("title") || "");
            }
        """))
    except Exception:
        return ""

def workday_country_is_united_states(page):
    for scope in get_apply_scopes(page):
        try:
            locator = scope.locator('button#country--country, [id="country--country"]').first
            if locator.count() == 0:
                continue
            display_norm = normalized_option_text(workday_country_display_text(locator))
            if display_norm in {"united states", "united states of america", "usa", "us", "u s", "u s a"}:
                return True
        except Exception:
            continue
    return False

def workday_selected_country_text(page):
    for scope in get_apply_scopes(page):
        try:
            locator = scope.locator('button#country--country, [id="country--country"]').first
            if locator.count():
                return workday_own_control_text(locator)
        except Exception:
            continue
    return ""

def workday_phone_country_code_is_us(page):
    for scope in get_apply_scopes(page):
        try:
            locator = scope.locator('input#phoneNumber--countryPhoneCode, [id="phoneNumber--countryPhoneCode"]').first
            if locator.count() == 0:
                continue
            text = workday_own_control_text(locator) or workday_control_context(locator)
            norm = normalized_option_text(text)
            return "united states of america 1" in norm or "united states 1" in norm or "united states of america" in norm
        except Exception:
            continue
    return False

def workday_selected_phone_country_code_text(page):
    for scope in get_apply_scopes(page):
        try:
            locator = scope.locator('input#phoneNumber--countryPhoneCode, [id="phoneNumber--countryPhoneCode"]').first
            if locator.count():
                return workday_own_control_text(locator) or workday_control_context(locator)
        except Exception:
            continue
    return ""

def close_workday_popups(page):
    for _ in range(3):
        try:
            page.keyboard.press("Escape", timeout=500)
            page.wait_for_timeout(180)
        except Exception:
            pass

def open_workday_control(page, locator, wait_ms=650):
    close_workday_popups(page)
    try:
        locator.evaluate("""
            el => {
              el.scrollIntoView({block: 'center', inline: 'nearest'});
              window.scrollBy(0, -80);
            }
        """)
    except Exception:
        try:
            locator.scroll_into_view_if_needed(timeout=1200)
        except Exception:
            pass
    page.wait_for_timeout(350)
    try:
        locator.click(timeout=2500, force=True)
    except Exception:
        try:
            locator.evaluate("el => el.click()")
        except Exception:
            return False
    page.wait_for_timeout(wait_ms)
    return True

def workday_locator_debug_snapshot(locator):
    try:
        return locator.evaluate("""
            el => {
              const box = el.getBoundingClientRect();
              return {
                text: String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim(),
                value: el.value || '',
                aria: el.getAttribute('aria-label') || '',
                expanded: el.getAttribute('aria-expanded') || '',
                controls: el.getAttribute('aria-controls') || '',
                top: Math.round(box.top),
                bottom: Math.round(box.bottom),
                visible: !!(box.width && box.height)
              };
            }
        """)
    except Exception as err:
        return {"error": str(err)}

def force_workday_phone_country_code_us(page):
    if workday_phone_country_code_is_us(page):
        return True
    for scope in get_apply_scopes(page):
        try:
            locator = scope.locator('input#phoneNumber--countryPhoneCode, [id="phoneNumber--countryPhoneCode"]').first
            if locator.count() == 0:
                continue
            for _ in range(3):
                try:
                    page.keyboard.press("Escape", timeout=500)
                    page.wait_for_timeout(250)
                except Exception:
                    pass
                locator.evaluate("el => el.scrollIntoView({block: 'center', inline: 'nearest'})")
                page.wait_for_timeout(500)
                clear_workday_selection_near(locator)
                page.wait_for_timeout(400)
                locator.click(timeout=2500, force=True)
                page.wait_for_timeout(400)
                try:
                    clicked = page.evaluate("""
                        () => {
                          const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
                          const visible = el => {
                            if (!el || !el.isConnected) return false;
                            const style = window.getComputedStyle(el);
                            const box = el.getBoundingClientRect();
                            return !!(box.width || box.height || el.getClientRects().length) &&
                              style.visibility !== 'hidden' && style.display !== 'none';
                          };
                          const wanted = new Set(['United States of America (+1)', 'United States (+1)']);
                          const nodes = Array.from(document.querySelectorAll('[role="option"], [role="listbox"] li, li'));
                          for (const node of nodes) {
                            if (visible(node) && wanted.has(clean(node.innerText || node.textContent))) {
                              node.click();
                              return true;
                            }
                          }
                          return false;
                        }
                    """)
                    if clicked:
                        page.wait_for_timeout(1000)
                        if workday_phone_country_code_is_us(page):
                            return True
                except Exception:
                    pass
                if click_workday_option(page, [
                    r"^United States of America \(\+1\)$",
                    r"^United States \(\+1\)$",
                ]):
                    page.wait_for_timeout(1000)
                    if workday_phone_country_code_is_us(page):
                        return True
                type_into_open_workday_prompt(page, "United States of America (+1)")
                if click_workday_option_with_scroll(page, [
                    r"^United States of America \(\+1\)$",
                    r"^United States \(\+1\)$",
                ], scroll_attempts=12):
                    page.wait_for_timeout(1500)
                    if workday_phone_country_code_is_us(page):
                        return True
                visual_result = visual_click_visible_text_option(page, "United States of America (+1)", reason="phone_country_code")
                if visual_result.get("acted"):
                    page.wait_for_timeout(1500)
                    if workday_phone_country_code_is_us(page):
                        return True
                locator.click(timeout=1500, force=True)
                page.wait_for_timeout(400)
                type_into_open_workday_prompt(page, "United States of America")
                if click_workday_option_with_scroll(page, [
                    r"^United States of America \(\+1\)$",
                    r"^United States \(\+1\)$",
                ], scroll_attempts=12):
                    page.wait_for_timeout(1500)
                    if workday_phone_country_code_is_us(page):
                        return True
                visual_result = visual_click_visible_text_option(page, "United States of America (+1)", reason="phone_country_code")
                if visual_result.get("acted"):
                    page.wait_for_timeout(1500)
                    if workday_phone_country_code_is_us(page):
                        return True
        except Exception:
            continue
    return workday_phone_country_code_is_us(page)

def force_workday_country_united_states(page):
    if workday_country_is_united_states(page):
        return True
    add_workday_country_debug("start", current=workday_selected_country_text(page))
    exact_country_texts = ["United States of America", "United States"]
    country_terms = [
        r"^United States of America$",
        r"^United States$",
        r"^United States of America \(USA\)$",
    ]
    for scope in get_apply_scopes(page):
        try:
            locator = scope.locator('button#country--country, [id="country--country"]').first
            if locator.count() == 0:
                add_workday_country_debug("country_locator_missing", scope_index=getattr(scope, "_impl_obj", None).__class__.__name__ if scope else "")
                continue
            add_workday_country_debug("country_locator_found", control=workday_locator_debug_snapshot(locator))
            for attempt in range(3):
                if not open_workday_control(page, locator):
                    add_workday_country_debug("open_failed", phase="exact_scroll", attempt=attempt, control=workday_locator_debug_snapshot(locator))
                    continue
                add_workday_country_debug(
                    "opened",
                    phase="exact_scroll",
                    attempt=attempt,
                    control=workday_locator_debug_snapshot(locator),
                    visible_options=visible_workday_option_texts(page, limit=15),
                )
                selection = select_workday_dropdown_exact_near_control(page, locator, exact_country_texts, scroll_attempts=55)
                add_workday_country_debug("exact_scroll_selection", attempt=attempt, selection=selection, current=workday_selected_country_text(page))
                if selection.get("clicked"):
                    page.wait_for_timeout(1500)
                    if workday_country_is_united_states(page):
                        close_workday_popups(page)
                        add_workday_country_debug("success", phase="exact_scroll", current=workday_selected_country_text(page))
                        return True
                close_workday_popups(page)

            # If the tenant exposes a real search input in the popup, use it.
            for attempt in range(2):
                if not open_workday_control(page, locator):
                    add_workday_country_debug("open_failed", phase="search", attempt=attempt, control=workday_locator_debug_snapshot(locator))
                    continue
                has_search = type_into_open_workday_prompt(page, "United States of America", allow_keyboard_type=False)
                add_workday_country_debug(
                    "search_attempt",
                    attempt=attempt,
                    has_search=has_search,
                    visible_options=visible_workday_option_texts(page, limit=15),
                )
                if has_search:
                    selection = select_workday_dropdown_exact_near_control(page, locator, exact_country_texts, scroll_attempts=8)
                    add_workday_country_debug("search_selection", attempt=attempt, selection=selection, current=workday_selected_country_text(page))
                    if selection.get("clicked"):
                        page.wait_for_timeout(1500)
                        if workday_country_is_united_states(page):
                            close_workday_popups(page)
                            add_workday_country_debug("success", phase="search", current=workday_selected_country_text(page))
                            return True
                close_workday_popups(page)

            # Keyboard typeahead is the last structured fallback. It must still
            # pass exact country validation before returning success.
            for attempt in range(4):
                if not open_workday_control(page, locator):
                    add_workday_country_debug("open_failed", phase="u_jump", attempt=attempt, control=workday_locator_debug_snapshot(locator))
                    continue
                add_workday_country_debug(
                    "opened",
                    phase="u_jump",
                    attempt=attempt,
                    control=workday_locator_debug_snapshot(locator),
                    visible_options=visible_workday_option_texts(page, limit=15),
                )
                try:
                    page.keyboard.type("u", delay=10)
                    page.wait_for_timeout(500)
                except Exception:
                    pass
                add_workday_country_debug(
                    "after_u",
                    attempt=attempt,
                    control=workday_locator_debug_snapshot(locator),
                    visible_options=visible_workday_option_texts(page, limit=15),
                )
                selection = select_workday_dropdown_exact_near_control(page, locator, exact_country_texts, scroll_attempts=18)
                add_workday_country_debug("u_selection", attempt=attempt, selection=selection, current=workday_selected_country_text(page))
                if selection.get("acted"):
                    page.wait_for_timeout(1500)
                    if workday_country_is_united_states(page):
                        close_workday_popups(page)
                        add_workday_country_debug("success", phase="u_selection", current=workday_selected_country_text(page))
                        return True
                if selection.get("clicked"):
                    page.wait_for_timeout(1500)
                    if workday_country_is_united_states(page):
                        close_workday_popups(page)
                        add_workday_country_debug("success", phase="u_selection", current=workday_selected_country_text(page))
                        return True

                # Last-resort keyboard confirmation for the U block. Different
                # tenants include slightly different U-country variants, so try
                # nearby offsets and validate after each Enter.
                close_workday_popups(page)
                for down_count in (4, 3, 5, 2, 6, 7):
                    if workday_country_is_united_states(page):
                        return True
                    if not open_workday_control(page, locator):
                        add_workday_country_debug("open_failed", phase="keyboard", attempt=attempt, down_count=down_count, control=workday_locator_debug_snapshot(locator))
                        continue
                    try:
                        page.keyboard.type("u", delay=10)
                        page.wait_for_timeout(250)
                        for _ in range(down_count):
                            page.keyboard.press("ArrowDown", timeout=500)
                            page.wait_for_timeout(80)
                        page.keyboard.press("Enter", timeout=700)
                        page.wait_for_timeout(1500)
                        add_workday_country_debug(
                            "keyboard_try",
                            attempt=attempt,
                            down_count=down_count,
                            current=workday_selected_country_text(page),
                            control=workday_locator_debug_snapshot(locator),
                            visible_options=visible_workday_option_texts(page, limit=10),
                        )
                        if workday_country_is_united_states(page):
                            close_workday_popups(page)
                            add_workday_country_debug("success", phase="keyboard", down_count=down_count, current=workday_selected_country_text(page))
                            return True
                    except Exception:
                        close_workday_popups(page)
                close_workday_popups(page)
        except Exception as err:
            add_workday_country_debug("exception", error=str(err))
            continue
    add_workday_country_debug("failed", current=workday_selected_country_text(page))
    return workday_country_is_united_states(page)

def is_workday_my_information_page(page):
    host = (urlparse(page.url or "").hostname or "").lower()
    if "myworkdayjobs.com" not in host:
        return False
    text = page_body_text(page, timeout=1200).lower()
    return "my information" in text and "how did you hear about us" in text and "country" in text

def is_workday_my_experience_page(page):
    host = (urlparse(page.url or "").hostname or "").lower()
    if "myworkdayjobs.com" not in host:
        return False
    text = page_body_text(page, timeout=1200).lower()
    return "my experience" in text and "work experience" in text and "education" in text

def clear_workday_selection_near(locator):
    try:
        return bool(locator.evaluate("""
            el => {
              const clickClear = root => {
                if (!root) return false;
                const candidates = Array.from(root.querySelectorAll(
                  'button, [role="button"], [aria-label], [data-automation-id*="delete"], [data-automation-id*="remove"]'
                ));
                for (const node of candidates) {
                  const text = String(node.innerText || node.textContent || node.getAttribute('aria-label') || node.getAttribute('data-automation-id') || '').trim();
                  if (/^(x|×)$/i.test(text) || /remove|delete|clear/i.test(text)) {
                    node.click();
                    return true;
                  }
                }
                return false;
              };
              let node = el;
              for (let depth = 0; node && depth < 5; depth++, node = node.parentElement) {
                if (clickClear(node)) return true;
              }
              return false;
            }
        """))
    except Exception:
        return False

def choose_workday_labeled_option(page, label_pattern, value):
    if not value:
        return False
    label_regex = re.compile(label_pattern, re.IGNORECASE)
    terms = list(discovery_option_terms(label_pattern, value))
    if normalized_option_text(value) == "mobile":
        terms.extend(["Mobile", "Cell", "Cellular"])
    if normalized_option_text(value) in US_STATE_ALIASES:
        terms.append(US_STATE_ALIASES[normalized_option_text(value)])
    for scope in get_apply_scopes(page):
        candidates = []
        try:
            candidates.append(scope.get_by_label(label_regex).first)
        except Exception:
            pass
        for label_text in [label_pattern.replace("\\", "")]:
            try:
                candidates.append(scope.locator(
                    f"xpath=//label[contains(normalize-space(.), \"{label_text}\")]/following::*[self::select or self::button or self::input or @role='combobox'][1]"
                ).first)
            except Exception:
                pass
        for locator in candidates:
            try:
                if locator.count() == 0 or not locator.is_visible(timeout=500):
                    continue
                if workday_locator_value_matches(locator, label_pattern, value):
                    return True
                try:
                    tag_name = (locator.evaluate("el => el.tagName.toLowerCase()") or "").lower()
                    if tag_name == "select":
                        options = locator.evaluate("""
                            el => Array.from(el.options || [])
                              .map(option => ({ value: option.value, text: option.innerText.trim(), disabled: option.disabled }))
                        """)
                        choice = choose_matching_option(options, label_pattern, value)
                        if choice:
                            locator.select_option(value=choice["value"], timeout=2000, force=True)
                            page.wait_for_timeout(700)
                            if workday_locator_value_matches(locator, label_pattern, value):
                                return True
                except Exception:
                    pass
                locator.scroll_into_view_if_needed(timeout=1000)
                clear_workday_selection_near(locator)
                page.wait_for_timeout(300)
                locator.click(timeout=2000)
                page.wait_for_timeout(500)
                try:
                    locator.fill(str(value), timeout=1000)
                    page.wait_for_timeout(500)
                except Exception:
                    pass
                if click_workday_option(page, terms):
                    page.wait_for_timeout(500)
                    if workday_locator_value_matches(locator, label_pattern, value):
                        return True
            except Exception:
                continue
    return False

def choose_workday_near_exact_text_option(page, label_patterns, value, avoid_patterns=None):
    if not value:
        return False
    avoid_patterns = avoid_patterns or []
    compiled = [re.compile(pattern, re.IGNORECASE) for pattern in label_patterns]
    avoid = [re.compile(pattern, re.IGNORECASE) for pattern in avoid_patterns]
    terms = list(discovery_option_terms(" ".join(label_patterns), value))
    if normalized_option_text(value) == "mobile":
        terms.extend(["Mobile", "Cell", "Cellular"])
    if normalized_option_text(value) in US_STATE_ALIASES:
        terms.append(US_STATE_ALIASES[normalized_option_text(value)])
    for scope in get_apply_scopes(page):
        try:
            labels = scope.locator('label, span, div, p')
            candidates = labels.evaluate_all("""
                nodes => nodes.map((node, index) => {
                  const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
                  const box = node.getBoundingClientRect();
                  return {
                    index,
                    text: clean(node.innerText || node.textContent || ""),
                    x: box.left,
                    y: box.top,
                    width: box.width,
                    height: box.height,
                    visible: !!(box.width && box.height)
                  };
                }).filter(item => item.visible && item.text && item.text.length < 240)
            """)
        except Exception:
            candidates = []
        for item in candidates[:300]:
            text = item.get("text") or ""
            if not any(pattern.search(text) for pattern in compiled):
                continue
            if any(pattern.search(text) for pattern in avoid):
                continue
            try:
                locator = scope.locator(
                    "xpath=(//*[normalize-space()=$label]/following::*[self::select or self::button or self::input or @role='combobox' or @aria-haspopup='listbox'][1])",
                    label=text
                ).first
            except Exception:
                safe_text = text.replace('"', '\\"')
                locator = scope.locator(
                    f'xpath=(//*[normalize-space()="{safe_text}"]/following::*[self::select or self::button or self::input or @role="combobox" or @aria-haspopup="listbox"][1])'
                ).first
            try:
                if locator.count() == 0 or not locator.is_visible(timeout=500):
                    continue
                if workday_locator_value_matches(locator, " ".join(label_patterns), value):
                    return True
                try:
                    tag_name = (locator.evaluate("el => el.tagName.toLowerCase()") or "").lower()
                    if tag_name == "select":
                        options = locator.evaluate("""
                            el => Array.from(el.options || [])
                              .map(option => ({ value: option.value, text: option.innerText.trim(), disabled: option.disabled }))
                        """)
                        choice = choose_matching_option(options, " ".join(label_patterns), value)
                        if choice:
                            locator.select_option(value=choice["value"], timeout=2000, force=True)
                            page.wait_for_timeout(700)
                            if workday_locator_value_matches(locator, " ".join(label_patterns), value):
                                return True
                except Exception:
                    pass
                label = locator_label(locator)
                context = " ".join([label, workday_control_context(locator)])
                if any(pattern.search(context) for pattern in avoid):
                    continue
                if not is_workday_select_control(locator, label, context):
                    continue
                locator.scroll_into_view_if_needed(timeout=1000)
                clear_workday_selection_near(locator)
                page.wait_for_timeout(300)
                locator.click(timeout=2000, force=True)
                page.wait_for_timeout(500)
                try:
                    locator.fill(str(value), timeout=1000)
                    page.wait_for_timeout(500)
                except Exception:
                    pass
                if click_workday_option(page, terms):
                    page.wait_for_timeout(700)
                    if workday_locator_value_matches(locator, " ".join(label_patterns), value):
                        return True
            except Exception:
                continue
    return False

def workday_control_context(locator):
    try:
        return compact_text(locator.evaluate("""
            el => {
              const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
              const rect = el.getBoundingClientRect();
              const chunks = [];
              let node = el;
              for (let depth = 0; node && depth < 5; depth++, node = node.parentElement) {
                const text = clean(node.innerText || node.textContent || "");
                if (text && text.length < 2500) chunks.push(text);
              }
              const all = Array.from(document.querySelectorAll("label, p, div, span"))
                .filter(node => {
                  const style = window.getComputedStyle(node);
                  if (style.visibility === "hidden" || style.display === "none") return false;
                  const box = node.getBoundingClientRect();
                  if (!box.width || !box.height) return false;
                  if (box.bottom > rect.top + 12) return false;
                  if (Math.abs(box.left - rect.left) > 180 && Math.abs(box.right - rect.right) > 180) return false;
                  return rect.top - box.bottom < 420;
                })
                .sort((a, b) => b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom);
              for (const node of all.slice(0, 12)) {
                const text = clean(node.innerText || node.textContent || "");
                if (text) chunks.push(text);
              }
              return chunks.join(" ");
            }
        """))
    except Exception:
        return ""

def workday_control_nearby_text(locator):
    try:
        return compact_text(locator.evaluate("""
            el => {
              const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
              const rect = el.getBoundingClientRect();
              const items = Array.from(document.querySelectorAll("label, p, div, span, li"))
                .filter(node => {
                  if (node === el || node.contains(el)) return false;
                  const style = window.getComputedStyle(node);
                  if (style.visibility === "hidden" || style.display === "none") return false;
                  const box = node.getBoundingClientRect();
                  if (!box.width || !box.height) return false;
                  if (box.bottom > rect.top + 20) return false;
                  if (rect.top - box.bottom > 520) return false;
                  const horizontallyRelevant = Math.abs(box.left - rect.left) < 180 || Math.abs(box.right - rect.right) < 260 || (box.left <= rect.left && box.right >= rect.left);
                  return horizontallyRelevant;
                })
                .sort((a, b) => b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom)
                .slice(0, 18)
                .map(node => clean(node.innerText || node.textContent || ""))
                .filter(Boolean);
              return items.join(" ");
            }
        """))
    except Exception:
        return ""

def text_similarity_match(needle, haystack):
    needle_norm = normalized_option_text(needle)
    haystack_norm = normalized_option_text(haystack)
    if not needle_norm or not haystack_norm:
        return False
    if needle_norm in haystack_norm or haystack_norm in needle_norm:
        return True
    needle_tokens = [token for token in needle_norm.split() if len(token) > 2]
    haystack_tokens = set(token for token in haystack_norm.split() if len(token) > 2)
    if not needle_tokens or not haystack_tokens:
        return False
    overlap = sum(1 for token in needle_tokens if token in haystack_tokens)
    return overlap >= max(4, int(len(needle_tokens) * 0.55))

def workday_select_like_controls(scope):
    selectors = [
        'button[aria-haspopup="listbox"]',
        '[role="combobox"]',
        'input[role="combobox"]',
        'button',
        'div[tabindex]',
    ]
    locators = []
    for selector in selectors:
        try:
            controls = scope.locator(selector)
            for index in range(min(controls.count(), 80)):
                locators.append(controls.nth(index))
        except Exception:
            continue
    return locators

def is_workday_select_control(locator, label="", context=""):
    combined = " ".join([label or "", context or ""])
    if re.search(r"\b(save|continue|submit|review|back|cancel|delete|withdraw)\b", label or "", re.IGNORECASE):
        return False
    try:
        role = locator.get_attribute("role", timeout=300) or ""
        aria_haspopup = locator.get_attribute("aria-haspopup", timeout=300) or ""
        aria_expanded = locator.get_attribute("aria-expanded", timeout=300) or ""
        data_automation = locator.get_attribute("data-automation-id", timeout=300) or ""
    except Exception:
        role = aria_haspopup = aria_expanded = data_automation = ""
    if role.lower() == "combobox" or "listbox" in aria_haspopup.lower() or aria_expanded:
        return True
    if re.search(r"\bselect\s+one\b|\bchoose\b", combined, re.IGNORECASE):
        return True
    return bool(re.search(r"(selectWidget|promptOption|dropdown)", data_automation, re.IGNORECASE))

def choose_workday_option_near_question(page, question_text, value, require_select_placeholder=False):
    if not question_text or not value:
        return False
    terms = list(discovery_option_terms(question_text, value))
    if normalized_option_text(value) in {"yes", "no"}:
        terms.insert(0, rf"^{re.escape(str(value).strip())}$")
    for scope in get_apply_scopes(page):
        for locator in workday_select_like_controls(scope):
            try:
                if locator.count() == 0 or not locator.is_visible(timeout=300):
                    continue
                label = locator_label(locator)
                context = " ".join([label, workday_control_context(locator)])
                if not is_workday_select_control(locator, label, context):
                    continue
                if require_select_placeholder and not re.search(r"\bselect\s+one\b", context, re.IGNORECASE):
                    continue
                if not text_similarity_match(question_text, context):
                    continue
                locator.scroll_into_view_if_needed(timeout=1000)
                locator.click(timeout=2000, force=True)
                page.wait_for_timeout(500)
                if click_workday_option(page, terms):
                    return True
            except Exception:
                continue
    return False

def saved_question_bank_answers(user_data):
    answers = []
    for answer in (user_data.get("field_answers") or {}).values():
        if not isinstance(answer, dict):
            continue
        field_text = str(answer.get("field") or "").strip()
        value = answer.get("value")
        if value in (None, "") or not field_text:
            continue
        if answer.get("question_bank") or len(field_text) >= 40:
            answers.append({
                "field": field_text,
                "value": str(value),
                "risk": answer.get("risk", "unknown"),
                "question_id": answer.get("question_id"),
            })
    return answers

def fill_workday_saved_question_answers(page, user_data):
    host = (urlparse(page.url or "").hostname or "").lower()
    if "myworkdayjobs.com" not in host:
        return []
    filled = []
    for answer in saved_question_bank_answers(user_data)[:40]:
        if choose_workday_option_near_question(page, answer["field"], answer["value"], require_select_placeholder=False):
            filled.append({
                "field": answer["field"][:180],
                "source": "question_bank_replay",
                "risk": answer.get("risk", "unknown")
            })
    return filled

def fill_workday_business_disclosure_no_fields(page, user_data):
    host = (urlparse(page.url or "").hostname or "").lower()
    if "myworkdayjobs.com" not in host:
        return []
    value = candidate_profile_value("government contractor business relationship conflict disclosure", "select", user_data)
    if normalized_option_text(value) != "no":
        return []
    filled = []
    for scope in get_apply_scopes(page):
        for locator in workday_select_like_controls(scope):
            try:
                if locator.count() == 0 or not locator.is_visible(timeout=300):
                    continue
                label = locator_label(locator)
                nearby = workday_control_nearby_text(locator)
                context = " ".join([label, nearby, workday_control_context(locator)])
                context_low = context.lower()
                if not is_workday_select_control(locator, label, context):
                    continue
                if "18 years" in nearby.lower() or "18 or older" in nearby.lower():
                    continue
                if not is_business_conflict_disclosure_question(context_low):
                    continue
                if workday_locator_value_matches(locator, "business conflict disclosure", "No"):
                    continue
                locator.scroll_into_view_if_needed(timeout=1000)
                locator.click(timeout=2000, force=True)
                page.wait_for_timeout(500)
                if click_workday_option(page, [r"^No$"]):
                    filled.append({
                        "field": compact_text(context)[:180],
                        "source": "workday_business_disclosure_no",
                        "risk": "high"
                    })
            except Exception:
                continue
    return filled

DECLINE_SELF_ID_OPTION_PATTERNS = [
    r"^\s*I\s+do\s+not\s+wish\s+to\s+answer\s*$",
    r"^\s*I\s+don't\s+wish\s+to\s+answer\s*$",
    r"^\s*I\s+do\s+not\s+want\s+to\s+answer\s*$",
    r"^\s*I\s+don't\s+want\s+to\s+answer\s*$",
    r"^\s*Decline\s+to\s+self[-\s]?identify\s*$",
    r"^\s*I\s+choose\s+not\s+to\s+self[-\s]?identify\s*$",
    r"^\s*I\s+do\s+not\s+wish\s+to\s+self[-\s]?identify\s*$",
    r"^\s*I\s+don't\s+wish\s+to\s+self[-\s]?identify\s*$",
    r"^\s*Choose\s+not\s+to\s+answer\s*$",
    r"^\s*Prefer\s+not\s+to\s+answer\s*$",
    r"^\s*I\s+prefer\s+not\s+to\s+answer\s*$",
]

def workday_locator_has_decline_self_id(locator):
    for value in [
        "I do not wish to answer",
        "I don't wish to answer",
        "I do not want to answer",
        "Decline to self-identify",
        "I choose not to self-identify",
        "Prefer not to answer",
    ]:
        if workday_locator_value_matches(locator, "self identification decline", value):
            return True
    return False

def choose_workday_decline_self_id_near_question(page, question_text):
    if not question_text:
        return False
    for scope in get_apply_scopes(page):
        for locator in workday_select_like_controls(scope):
            try:
                if locator.count() == 0 or not locator.is_visible(timeout=300):
                    continue
                label = locator_label(locator)
                context = " ".join([label, workday_control_context(locator), workday_control_nearby_text(locator)])
                if not is_workday_select_control(locator, label, context):
                    continue
                if not text_similarity_match(question_text, context):
                    continue
                if workday_locator_has_decline_self_id(locator):
                    return True
                locator.scroll_into_view_if_needed(timeout=1000)
                clear_workday_selection_near(locator)
                page.wait_for_timeout(250)
                locator.click(timeout=2500, force=True)
                page.wait_for_timeout(600)
                if click_workday_option(page, DECLINE_SELF_ID_OPTION_PATTERNS):
                    page.wait_for_timeout(800)
                    if workday_locator_has_decline_self_id(locator):
                        return True
                    return True
            except Exception:
                continue
    return False

def fill_workday_voluntary_disclosure_declines(page, user_data):
    host = (urlparse(page.url or "").hostname or "").lower()
    if "myworkdayjobs.com" not in host:
        return []
    if not is_workday_my_experience_page(page):
        return []
    body_text = page_body_text(page, timeout=1500)
    body_low = body_text.lower()
    if not any(token in body_low for token in [
        "voluntary disclosures",
        "personal data statement",
        "self identify",
        "self-identification",
        "what is your gender",
        "what is your ethnicity",
        "are you a veteran",
        "disability",
    ]):
        return []
    filled = []
    for question in [
        "What is your gender?",
        "What is your ethnicity?",
        "Are you a veteran?",
        "Please select your veteran status",
        "Voluntary Self-Identification of Disability",
        "Disability status",
        "Do you have a disability?",
    ]:
        if question.lower().rstrip("?") not in body_low and not any(word in body_low for word in question.lower().split()[:3]):
            continue
        if choose_workday_decline_self_id_near_question(page, question):
            filled.append({
                "field": question,
                "value": "Decline to answer",
                "source": "self_identification_decline",
                "risk": "high",
                "requires_confirmation": False,
            })
    return filled

def fill_workday_self_identification_signature_fields(page, user_data):
    host = (urlparse(page.url or "").hostname or "").lower()
    if "myworkdayjobs.com" not in host:
        return []
    body_low = page_body_text(page, timeout=1200).lower()
    if "self identify" not in body_low and "self-identification of disability" not in body_low:
        return []
    filled = []
    today = datetime.today().date()
    full_name = " ".join(part for part in [user_data.get("first_name"), user_data.get("last_name")] if part).strip()
    if full_name and fill_first_visible_locator(page, [
        'input#selfIdentifiedDisabilityData--name',
        'input[id*="selfIdentifiedDisabilityData--name"]',
    ], full_name):
        filled.append({"field": "Self Identify Name", "source": "profile", "risk": "low"})
    date_fields = [
        ("Month", f"{today.month:02d}", [
            'input[id*="selfIdentifiedDisabilityData--dateSignedOn"][id*="Month"]',
        ]),
        ("Day", f"{today.day:02d}", [
            'input[id*="selfIdentifiedDisabilityData--dateSignedOn"][id*="Day"]',
        ]),
        ("Year", str(today.year), [
            'input[id*="selfIdentifiedDisabilityData--dateSignedOn"][id*="Year"]',
        ]),
    ]
    for label, value, selectors in date_fields:
        if fill_first_visible_locator(page, selectors, value):
            filled.append({"field": f"Self Identify Date {label}", "source": "current_date", "risk": "low"})
    return filled

def fill_workday_provisional_select_answers(page, user_data):
    if not user_data.get("allow_provisional_answers"):
        return []
    host = (urlparse(page.url or "").hostname or "").lower()
    if "myworkdayjobs.com" not in host or not page_has_validation_errors(page):
        return []
    filled = []
    for scope in get_apply_scopes(page):
        for locator in workday_select_like_controls(scope):
            try:
                if locator.count() == 0 or not locator.is_visible(timeout=300):
                    continue
                label = locator_label(locator)
                context = " ".join([label, workday_control_context(locator)])
                context_low = context.lower()
                if not is_workday_select_control(locator, label, context):
                    continue
                if not re.search(r"\bselect\s+one\b", context_low):
                    continue
                if re.search(r"(gender|race|ethnicity|hispanic|veteran|disability|voluntary self|self-identification|self identification|eeo|equal opportunity)", context_low):
                    continue
                if is_business_conflict_disclosure_question(context_low):
                    continue
                nearby = workday_control_nearby_text(locator).lower()
                answer = "Yes" if re.search(r"\b18\s+years?\b|\b18\s+or\s+older\b", nearby) else "No"
                locator.scroll_into_view_if_needed(timeout=1000)
                locator.click(timeout=2000, force=True)
                page.wait_for_timeout(500)
                if click_workday_option(page, [rf"^{answer}$"]):
                    filled.append({
                        "field": compact_text(context)[:240],
                        "value": answer,
                        "source": "provisional_answer",
                        "risk": "high" if re.search(r"(18 years|older|authorized|sponsor|visa|security|government|conflict|contractor|official|family member|relative|llp)", context_low) else "medium",
                        "requires_confirmation": True,
                    })
            except Exception:
                continue
    return filled

def fill_workday_age_over_18(page, user_data):
    host = (urlparse(page.url or "").hostname or "").lower()
    if "myworkdayjobs.com" not in host:
        return []
    value = candidate_profile_value("Are you 18 years or older?", "select", user_data)
    source = "profile"
    requires_confirmation = False
    if not value and user_data.get("allow_provisional_answers"):
        value = "Yes"
        source = "provisional_answer"
        requires_confirmation = True
    if normalized_option_text(value) not in {"yes", "no"}:
        return []
    for question in [
        "Are you 18 years or older?",
        "18 years or older",
        "18 or older",
    ]:
        if choose_workday_option_near_question(page, question, value, require_select_placeholder=False):
            return [{
                "field": "Are you 18 years or older?",
                "value": value,
                "source": source,
                "risk": "high",
                "requires_confirmation": requires_confirmation,
            }]
    return []

def resume_has_terms(resume_text, terms):
    low = str(resume_text or "").lower()
    return any(term.lower() in low for term in terms)

def fill_workday_resume_question_answers(page, user_data):
    host = (urlparse(page.url or "").hostname or "").lower()
    if "myworkdayjobs.com" not in host:
        return []
    text = page_body_text(page, timeout=1500)
    text_low = text.lower()
    if "application questions" not in text_low:
        return []
    resume_text = user_data.get("resume_text") or ""
    answers = []

    def add(question, value, source="resume_question_rule", risk="medium", requires_confirmation=False):
        if value in (None, ""):
            return
        answers.append({
            "question": question,
            "value": value,
            "source": source,
            "risk": risk,
            "requires_confirmation": requires_confirmation,
        })

    add("What is your highest level of education completed?", "Bachelor's Degree", risk="medium")

    citizenship_value = user_data.get("us_citizenship") or user_data.get("citizenship_us")
    if not citizenship_value and user_data.get("allow_provisional_answers"):
        citizenship_value = "No"
        citizenship_source = "provisional_answer"
        citizenship_confirm = True
    else:
        citizenship_source = "profile"
        citizenship_confirm = False
    add(
        "Do you have the ability to obtain a U.S. Security Clearance for which the U.S. Government requires U.S. Citizenship?",
        citizenship_value,
        source=citizenship_source,
        risk="high",
        requires_confirmation=citizenship_confirm,
    )

    if resume_has_terms(resume_text, ["c++", "c/c++"]):
        add("Are you proficient in coding C++?", "Yes", risk="medium")
    if resume_has_terms(resume_text, ["bachelor of science", "b.s.", "bs computer science", "bachelor"]):
        add("Do you have a Bachelor of Science degree from an accredited course of study in engineering", "Yes", risk="medium")
    if resume_has_terms(resume_text, ["python"]):
        add("Do you have good knowledge and skills in Python?", "Yes", risk="medium")
    if user_data.get("allow_provisional_answers"):
        add(
            "How many years of experience in using the Qt cross-platform application development framework for creating graphical user interfaces?",
            "0",
            source="provisional_answer",
            risk="medium",
            requires_confirmation=True,
        )
    if resume_has_terms(resume_text, ["linux", "windows", "systems programming", "c/c++", "c++"]):
        add("Do you have ability to work in both a Windows and Linux environment?", "Yes", risk="medium")

    filled = []
    for answer in answers:
        if choose_workday_option_near_question(page, answer["question"], answer["value"], require_select_placeholder=False):
            filled.append({
                "field": answer["question"][:180],
                "value": answer["value"],
                "source": answer["source"],
                "risk": answer["risk"],
                "requires_confirmation": answer.get("requires_confirmation", False),
            })
    return filled

MONTH_ALIASES = {
    "jan": "January", "january": "January",
    "feb": "February", "february": "February",
    "mar": "March", "march": "March",
    "apr": "April", "april": "April",
    "may": "May",
    "jun": "June", "june": "June",
    "jul": "July", "july": "July",
    "aug": "August", "august": "August",
    "sep": "September", "sept": "September", "september": "September",
    "oct": "October", "october": "October",
    "nov": "November", "november": "November",
    "dec": "December", "december": "December",
}

MONTH_NUMBERS = {
    "January": "01", "February": "02", "March": "03", "April": "04",
    "May": "05", "June": "06", "July": "07", "August": "08",
    "September": "09", "October": "10", "November": "11", "December": "12",
}

def month_number(value):
    month = MONTH_ALIASES.get(str(value or "").strip().lower().replace(".", ""), str(value or "").strip())
    return MONTH_NUMBERS.get(month, "")

def parse_resume_date_range(text):
    value = str(text or "")
    parts = re.split(r"\s+(?:-|–|—|to)\s+", value, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) < 2:
        return {}, {}

    def parse_part(part):
        low = part.strip().lower().replace(".", "")
        if re.search(r"present|current|now", low):
            return {"present": True}
        month = ""
        year = ""
        month_match = re.search(r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b", low)
        if month_match:
            month = MONTH_ALIASES.get(month_match.group(1), "")
        year_match = re.search(r"\b(20\d{2}|19\d{2})\b", low)
        if year_match:
            year = year_match.group(1)
        return {
            "month": month,
            "month_number": month_number(month),
            "year": year,
            "date": f"{month_number(month) or '01'}/{year}" if year else "",
            "present": False,
        }

    return parse_part(parts[0]), parse_part(parts[1])

def explicit_mmddyyyy_date(value):
    text = str(value or "").strip()
    if not text:
        return None
    month = day = year = ""
    match = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d{2}|19\d{2})\b", text)
    if match:
        month, day, year = match.groups()
    if not match:
        match = re.search(r"\b(20\d{2}|19\d{2})-(\d{1,2})-(\d{1,2})\b", text)
        if match:
            year, month, day = match.groups()
    if not match:
        month_names = "|".join(sorted(MONTH_ALIASES.keys(), key=len, reverse=True))
        match = re.search(rf"\b({month_names})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(20\d{{2}}|19\d{{2}})\b", text, re.IGNORECASE)
        if match:
            month = month_number(match.group(1))
            day = match.group(2)
            year = match.group(3)
    if not (month and day and year):
        return None
    try:
        month_i = int(month)
        day_i = int(day)
        year_i = int(year)
    except Exception:
        return None
    if not (1 <= month_i <= 12 and 1 <= day_i <= 31 and 1900 <= year_i <= 2100):
        return None
    return {
        "month_number": f"{month_i:02d}",
        "day": f"{day_i:02d}",
        "year": str(year_i),
        "date": f"{month_i:02d}/{day_i:02d}/{year_i}",
    }

def profile_start_date_value(user_data):
    data = user_data or {}
    for key in ["start_date", "earliest_start_date"]:
        if data.get(key):
            return data.get(key), key
    library = data.get("application_profile_library")
    if isinstance(library, dict):
        availability = library.get("availability")
        if isinstance(availability, dict) and availability.get("start_date"):
            return availability.get("start_date"), "application_profile_library.availability.start_date"
    return None, ""

def sanitize_workday_long_text(value):
    text = compact_text(value)
    text = text.replace("→", " to ").replace("–", "-").replace("—", "-")
    text = re.sub(r'[<>\[\]"{}\\\\]', "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:1800]

def parse_profile_work_experiences(user_data, max_items=2):
    entries = (user_data or {}).get("work_experience_entries") or (user_data or {}).get("experience_entries")
    if isinstance(entries, dict):
        entries = [entries]
    parsed = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        start = {
            "month": entry.get("start_month") or "",
            "month_number": month_number(entry.get("start_month")),
            "year": entry.get("start_year") or "",
            "date": f"{month_number(entry.get('start_month')) or '01'}/{entry.get('start_year')}" if entry.get("start_year") else "",
            "present": False,
        }
        current = bool(entry.get("current"))
        end = {
            "month": "" if current else (entry.get("end_month") or ""),
            "month_number": "" if current else month_number(entry.get("end_month")),
            "year": "" if current else (entry.get("end_year") or ""),
            "date": "" if current else (f"{month_number(entry.get('end_month')) or '01'}/{entry.get('end_year')}" if entry.get("end_year") else ""),
            "present": current,
        }
        parsed.append({
            "company": entry.get("company") or "",
            "location": entry.get("location") or "",
            "title": entry.get("title") or entry.get("job_title") or entry.get("role") or "",
            "date_text": entry.get("date_text") or "",
            "start": start,
            "end": end,
            "description": sanitize_workday_long_text(entry.get("description") or entry.get("summary") or ""),
        })
        if len(parsed) >= max_items:
            break
    return [
        entry for entry in parsed
        if entry.get("company") or entry.get("title") or entry.get("description")
    ]


def parse_resume_work_experiences(resume_text, max_items=2, user_data=None):
    profile_entries = parse_profile_work_experiences(user_data, max_items=max_items)
    if profile_entries:
        return profile_entries
    raw_text = str(resume_text or "")
    lines = [line.rstrip() for line in raw_text.splitlines()]
    entries = []
    in_section = False
    i = 0
    stop_re = re.compile(r"^(research|projects?|education|technical skills|skills|summary|profile|publications?)\b", re.IGNORECASE)
    start_re = re.compile(r"^(industrial experience|work experience|professional experience|experience)\b", re.IGNORECASE)
    while i < len(lines):
        line = lines[i].strip()
        if not in_section:
            if start_re.search(line):
                in_section = True
            i += 1
            continue
        if not line:
            i += 1
            continue
        if stop_re.search(line):
            break
        if "|" not in line or i + 1 >= len(lines):
            i += 1
            continue
        company_parts = [part.strip() for part in line.split("|")]
        next_line = lines[i + 1].strip()
        if "|" not in next_line:
            i += 1
            continue
        role_parts = [part.strip() for part in next_line.split("|")]
        date_text = role_parts[-1] if role_parts else ""
        if not re.search(r"\b(20\d{2}|19\d{2}|present|current)\b", date_text, re.IGNORECASE):
            i += 1
            continue
        bullets = []
        j = i + 2
        while j < len(lines):
            bullet = lines[j].strip()
            if not bullet:
                j += 1
                continue
            if stop_re.search(bullet):
                break
            if "|" in bullet and j + 1 < len(lines) and "|" in lines[j + 1]:
                break
            if bullet.startswith(("-", "•", "*")):
                bullets.append(re.sub(r"^[-•*]\s*", "", bullet).strip())
            j += 1
        start_date, end_date = parse_resume_date_range(date_text)
        entries.append({
            "company": company_parts[0],
            "location": company_parts[-1] if len(company_parts) > 1 else "",
            "title": role_parts[0],
            "date_text": date_text,
            "start": start_date,
            "end": end_date,
            "description": sanitize_workday_long_text(" ".join(bullets[:6])),
        })
        if len(entries) >= max_items:
            break
        i = max(j, i + 2)
    if entries:
        return entries

    text = re.sub(r"\s+", " ", raw_text)
    fallback_match = re.search(
        r"(Volcengine\s*\(ByteDance\))\s*(Beijing,\s*China)\s*"
        r"(Software Engineer Intern)\s*(May\s+2024\s*[–—-]\s*Aug\.?\s+2024)",
        text,
        re.IGNORECASE,
    )
    if fallback_match:
        start_date, end_date = parse_resume_date_range(fallback_match.group(4).replace("–", "-").replace("—", "-"))
        bullets = []
        for pattern in [
            r"Core Platform & Canary Routing:(.*?)(?:L1/L2 Caching|AsyncTool Workflow|Distributed Dispatcher|Milvus & Compliance|RESEARCH)",
            r"L1/L2 Caching & Flexible Billing:(.*?)(?:AsyncTool Workflow|Distributed Dispatcher|Milvus & Compliance|RESEARCH)",
            r"AsyncTool Workflow Orchestration:(.*?)(?:Distributed Dispatcher|Milvus & Compliance|RESEARCH)",
        ]:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                bullets.append(sanitize_workday_long_text(match.group(1))[:360])
        entries.append({
            "company": fallback_match.group(1),
            "location": fallback_match.group(2),
            "title": fallback_match.group(3),
            "date_text": fallback_match.group(4),
            "start": start_date,
            "end": end_date,
            "description": sanitize_workday_long_text(" ".join(bullets[:4])),
        })
    return entries

def parse_resume_education(resume_text, user_data=None):
    profile = dict(DEFAULT_EDUCATION_PROFILE)
    overrides = {
        "school": user_data.get("education_school") if user_data else None,
        "degree": user_data.get("education_degree") if user_data else None,
        "field": user_data.get("education_field") if user_data else None,
        "location": user_data.get("education_location") if user_data else None,
        "start_month": user_data.get("education_start_month") if user_data else None,
        "start_year": user_data.get("education_start_year") if user_data else None,
        "end_month": user_data.get("education_end_month") if user_data else None,
        "end_year": user_data.get("education_end_year") if user_data else None,
    }
    profile.update({key: value for key, value in overrides.items() if value})
    text = re.sub(r"\s+", " ", str(resume_text or ""))
    school_match = re.search(r"(University of Illinois at Urbana-?Champaign)\s*(Champaign,\s*IL)?", text, re.IGNORECASE)
    if school_match:
        profile["school"] = "University of Illinois at Urbana-Champaign"
        if school_match.group(2):
            profile["location"] = "Champaign, IL"
    degree_match = re.search(r"(Bachelor of Science in Computer Science and Linguistics)\s+Aug\.?\s+2021\s*[–—-]\s*(May|Dec(?:ember)?|December)\.?\s+2026", text, re.IGNORECASE)
    if degree_match:
        profile["degree"] = "Bachelor of Science in Computer Science and Linguistics"
        profile["field"] = "Computer Science and Linguistics"
    # User explicitly corrected the graduation date to Dec 2026; keep this as the canonical application value.
    profile["end_month"] = "December"
    profile["end_year"] = "2026"
    return profile

def fill_workday_labeled_any(page, patterns, value, select=False):
    if not value:
        return False
    for pattern in patterns:
        if select:
            if choose_workday_labeled_option(page, pattern, value):
                return True
        if fill_labeled_text_control(page, pattern, value):
            return True
    return False

WORKDAY_EDUCATION_SECTION_STOPS = [
    "Certifications and Licenses",
    "Certifications",
    "Licenses",
    "Languages",
    "Skills",
    "Resume/CV",
    "Resume",
    "CV",
]
WORKDAY_EDUCATION_ADD_DEBUG = []

def workday_education_school_values(school):
    values = [
        school,
        "University of Illinois at Urbana-Champaign",
        "University of Illinois Urbana-Champaign",
        "University of Illinois Urbana Champaign",
        "UIUC",
    ]
    return list(dict.fromkeys([value for value in values if value]))

def clear_workday_element_value(element):
    if not element:
        return False
    try:
        element.evaluate("""
            el => {
              const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
              const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
              if (setter) setter.call(el, "");
              else el.value = "";
              el.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "deleteContentBackward", data: null}));
              el.dispatchEvent(new Event("change", {bubbles: true}));
              el.dispatchEvent(new Event("blur", {bubbles: true}));
            }
        """)
        return True
    except Exception:
        try:
            element.fill("", timeout=1000)
            return True
        except Exception:
            return False

def workday_education_school_input_matches(page, school):
    values = [normalized_option_text(value) for value in workday_education_school_values(school)]
    values = [value for value in values if value]
    if not values:
        return False
    try:
        observed = page.evaluate("""
            ({sectionName, stopNames}) => {
              const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
              const visible = el => {
                if (!el || !el.isConnected) return false;
                const style = window.getComputedStyle(el);
                const box = el.getBoundingClientRect();
                return !!(box.width && box.height) && style.visibility !== "hidden" && style.display !== "none";
              };
              const norm = value => clean(value).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
              const follows = (a, b) => !!(a && b && (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING));
              const before = (a, b) => !!(a && b && (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING));
              const domSort = (a, b) => {
                if (a === b) return 0;
                return follows(a, b) ? -1 : 1;
              };
              const textNodes = Array.from(document.querySelectorAll("h1,h2,h3,h4,h5,h6,div,span,p,label"))
                .filter(visible)
                .filter(node => clean(node.innerText || node.textContent || "").length < 800);
              const heading = textNodes
                .filter(node => norm(node.innerText || node.textContent || "") === norm(sectionName))
                .sort(domSort)
                .pop();
              if (!heading) return [];
              const stopNorms = (stopNames || []).map(norm).filter(Boolean);
              const stop = textNodes
                .filter(node => follows(heading, node))
                .filter(node => stopNorms.includes(norm(node.innerText || node.textContent || "")))
                .sort(domSort)[0] || null;
              const inSection = el => follows(heading, el) && (!stop || before(el, stop));
              const labelFor = el => {
                const chunks = [
                  el.id || "",
                  el.getAttribute("name") || "",
                  el.getAttribute("aria-label") || "",
                  el.getAttribute("placeholder") || "",
                ];
                if (el.id) {
                  const explicit = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                  if (explicit) chunks.push(clean(explicit.innerText || explicit.textContent || ""));
                }
                return norm(chunks.join(" "));
              };
              return Array.from(document.querySelectorAll("input, textarea, button, [role='combobox']"))
                .filter(visible)
                .filter(inSection)
                .filter(el => /school|university|institution/.test(labelFor(el)))
                .map(el => {
                  const chunks = [
                    clean(el.value),
                    clean(el.innerText || el.textContent || ""),
                    clean(el.getAttribute("aria-label") || ""),
                  ];
                  let node = el.parentElement;
                  for (let depth = 0; node && depth < 2; depth++, node = node.parentElement) {
                    const text = clean(node.innerText || node.textContent || "");
                    if (text && text.length < 500) chunks.push(text);
                  }
                  return chunks.filter(Boolean).join(" ");
                });
            }
        """, {"sectionName": "Education", "stopNames": WORKDAY_EDUCATION_SECTION_STOPS})
        observed_norm = normalized_option_text(" ".join(observed or []))
        return any(value in observed_norm for value in values)
    except Exception:
        return False

def clear_workday_education_school_inputs(page):
    selectors = [
        'input[id*="education-"][id*="schoolName"]',
        'input[id*="education-"][id*="school"]',
        'input[id*="education"][id*="institution"]',
        'input[name*="education"][name*="school"]',
        'input[name*="education"][name*="institution"]',
    ]
    cleared = False
    for scope in get_apply_scopes(page):
        for selector in selectors:
            try:
                locators = scope.locator(selector)
                for index in range(min(locators.count(), 10)):
                    locator = locators.nth(index)
                    if locator.is_visible(timeout=300):
                        cleared = clear_workday_element_value(locator) or cleared
            except Exception:
                continue
    return cleared

def fill_workday_education_school_text_fallback(page, school):
    school_value = "University of Illinois at Urbana-Champaign" if "university of illinois" in normalized_option_text(school) else school
    element = find_workday_section_control(
        page,
        "Education",
        WORKDAY_EDUCATION_SECTION_STOPS,
        "input:not([type='hidden']):not([type='file']), textarea",
        [r"School", r"University", r"Institution"],
    )
    if not element:
        return False
    try:
        clear_workday_element_value(element)
        page.wait_for_timeout(250)
        element.fill(str(school_value), timeout=2500)
        element.evaluate("""
            el => {
              el.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertText", data: el.value}));
              el.dispatchEvent(new Event("change", {bubbles: true}));
              el.dispatchEvent(new Event("blur", {bubbles: true}));
            }
        """)
        page.wait_for_timeout(700)
        return workday_education_school_input_matches(page, school_value)
    except Exception:
        return False

def workday_education_school_present(page, school):
    return workday_education_school_input_matches(page, school)

def reset_workday_education_add_debug():
    global WORKDAY_EDUCATION_ADD_DEBUG
    WORKDAY_EDUCATION_ADD_DEBUG = []

def add_workday_education_add_debug(event, **details):
    try:
        WORKDAY_EDUCATION_ADD_DEBUG.append({
            "event": event,
            "ts": datetime.utcnow().isoformat() + "Z",
            **details,
        })
    except Exception:
        pass

def workday_section_query_js():
    return """
        ({sectionName, stopNames, selector, labelPatterns}) => {
          const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
          const visible = el => {
            if (!el || !el.isConnected) return false;
            const style = window.getComputedStyle(el);
            const box = el.getBoundingClientRect();
            return !!(box.width && box.height) && style.visibility !== "hidden" && style.display !== "none";
          };
          const norm = value => clean(value).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
          const follows = (a, b) => !!(a && b && (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING));
          const before = (a, b) => !!(a && b && (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING));
          const domSort = (a, b) => {
            if (a === b) return 0;
            return follows(a, b) ? -1 : 1;
          };
          const textNodes = Array.from(document.querySelectorAll("h1,h2,h3,h4,h5,h6,div,span,p"))
            .filter(visible)
            .filter(node => clean(node.innerText || node.textContent || "").length < 800);
          const headings = textNodes
            .filter(node => norm(node.innerText || node.textContent || "") === norm(sectionName))
            .sort(domSort);
          const heading = headings[headings.length - 1] || null;
          if (!heading) return null;
          const stopNorms = (stopNames || []).map(norm).filter(Boolean);
          const stop = textNodes
            .filter(node => follows(heading, node))
            .filter(node => stopNorms.includes(norm(node.innerText || node.textContent || "")))
            .sort(domSort)[0] || null;
          const inSection = el => follows(heading, el) && (!stop || before(el, stop));
          const labelFor = el => {
            const chunks = [
              el.id || "",
              el.getAttribute("name") || "",
              el.getAttribute("aria-label") || "",
              el.getAttribute("placeholder") || "",
              el.getAttribute("data-automation-id") || "",
            ];
            if (el.id) {
              const explicit = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
              if (explicit && inSection(explicit)) chunks.push(clean(explicit.innerText || explicit.textContent || ""));
            }
            let node = el;
            for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {
              const text = clean(node.innerText || node.textContent || "");
              if (text && text.length < 900) chunks.push(text);
            }
            const preceding = textNodes
              .filter(node => inSection(node) && follows(node, el))
              .slice(-10)
              .map(node => clean(node.innerText || node.textContent || ""))
              .filter(Boolean);
            chunks.push(...preceding);
            return clean(chunks.filter(Boolean).join(" "));
          };
          const regexes = (labelPatterns || []).map(pattern => new RegExp(pattern, "i"));
          const controls = Array.from(document.querySelectorAll(selector))
            .filter(visible)
            .filter(inSection)
            .filter(el => !el.disabled && el.getAttribute("aria-disabled") !== "true")
            .sort(domSort);
          if (!regexes.length) return controls[0] || null;
          for (const control of controls) {
            const context = labelFor(control);
            if (regexes.some(regex => regex.test(context))) return control;
          }
          return null;
        }
    """

def workday_section_field_count(page, section_name, stop_names):
    try:
        return int(page.evaluate("""
            ({sectionName, stopNames}) => {
              const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
              const visible = el => {
                if (!el || !el.isConnected) return false;
                const style = window.getComputedStyle(el);
                const box = el.getBoundingClientRect();
                return !!(box.width && box.height) && style.visibility !== "hidden" && style.display !== "none";
              };
              const norm = value => clean(value).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
              const follows = (a, b) => !!(a && b && (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING));
              const before = (a, b) => !!(a && b && (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING));
              const domSort = (a, b) => {
                if (a === b) return 0;
                return follows(a, b) ? -1 : 1;
              };
              const textNodes = Array.from(document.querySelectorAll("h1,h2,h3,h4,h5,h6,div,span,p"))
                .filter(visible)
                .filter(node => clean(node.innerText || node.textContent || "").length < 800);
              const headings = textNodes
                .filter(node => norm(node.innerText || node.textContent || "") === norm(sectionName))
                .sort(domSort);
              const heading = headings[headings.length - 1] || null;
              if (!heading) return 0;
              const stopNorms = (stopNames || []).map(norm).filter(Boolean);
              const stop = textNodes
                .filter(node => follows(heading, node))
                .filter(node => stopNorms.includes(norm(node.innerText || node.textContent || "")))
                .sort(domSort)[0] || null;
              const inSection = el => follows(heading, el) && (!stop || before(el, stop));
              return Array.from(document.querySelectorAll(
                'input:not([type="hidden"]):not([type="file"]), textarea, select, button[aria-haspopup="listbox"], [role="combobox"]'
              ))
                .filter(visible)
                .filter(inSection)
                .filter(el => {
                  const text = clean(el.innerText || el.textContent || el.value || el.getAttribute("aria-label") || "");
                  if (/^(add|add another|save and continue|back|delete)$/i.test(text)) return false;
                  return !/resume|cv|upload/i.test(`${el.id || ""} ${el.getAttribute("name") || ""} ${el.getAttribute("data-automation-id") || ""}`);
                })
                .length;
            }
        """, {"sectionName": section_name, "stopNames": stop_names}))
    except Exception:
        return 0

def workday_section_contains_text(page, section_name, stop_names, expected_text):
    if not expected_text:
        return False
    try:
        section_text = page.evaluate("""
            ({sectionName, stopNames}) => {
              const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
              const visible = el => {
                if (!el || !el.isConnected) return false;
                const style = window.getComputedStyle(el);
                const box = el.getBoundingClientRect();
                return !!(box.width && box.height) && style.visibility !== "hidden" && style.display !== "none";
              };
              const norm = value => clean(value).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
              const follows = (a, b) => !!(a && b && (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING));
              const before = (a, b) => !!(a && b && (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING));
              const domSort = (a, b) => {
                if (a === b) return 0;
                return follows(a, b) ? -1 : 1;
              };
              const textNodes = Array.from(document.querySelectorAll("h1,h2,h3,h4,h5,h6,div,span,p,label,input,textarea,button"))
                .filter(visible)
                .filter(node => clean(node.innerText || node.textContent || node.value || "").length < 1200);
              const headings = textNodes
                .filter(node => norm(node.innerText || node.textContent || "") === norm(sectionName))
                .sort(domSort);
              const heading = headings[headings.length - 1] || null;
              if (!heading) return "";
              const stopNorms = (stopNames || []).map(norm).filter(Boolean);
              const stop = textNodes
                .filter(node => follows(heading, node))
                .filter(node => stopNorms.includes(norm(node.innerText || node.textContent || "")))
                .sort(domSort)[0] || null;
              return textNodes
                .filter(node => follows(heading, node) && (!stop || before(node, stop)))
                .map(node => clean(node.innerText || node.textContent || node.value || ""))
                .filter(Boolean)
                .join(" ");
            }
        """, {"sectionName": section_name, "stopNames": stop_names})
        return normalized_option_text(expected_text) in normalized_option_text(section_text)
    except Exception:
        return False

def workday_education_debug_snapshot(page):
    try:
        return page.evaluate("""
            ({sectionName, stopNames}) => {
              const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
              const visible = el => {
                if (!el || !el.isConnected) return false;
                const style = window.getComputedStyle(el);
                const box = el.getBoundingClientRect();
                return !!(box.width && box.height) && style.visibility !== "hidden" && style.display !== "none";
              };
              const norm = value => clean(value).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
              const follows = (a, b) => !!(a && b && (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING));
              const before = (a, b) => !!(a && b && (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING));
              const domSort = (a, b) => {
                if (a === b) return 0;
                return follows(a, b) ? -1 : 1;
              };
              const boxOf = el => {
                const box = el.getBoundingClientRect();
                return {
                  x: Math.round(box.x),
                  y: Math.round(box.y),
                  top: Math.round(box.top),
                  bottom: Math.round(box.bottom),
                  width: Math.round(box.width),
                  height: Math.round(box.height),
                };
              };
              const textNodes = Array.from(document.querySelectorAll("h1,h2,h3,h4,h5,h6,div,span,p,label"))
                .filter(visible)
                .filter(node => clean(node.innerText || node.textContent || "").length < 900);
              const headings = textNodes
                .filter(node => norm(node.innerText || node.textContent || "") === norm(sectionName))
                .sort(domSort);
              const heading = headings[headings.length - 1] || null;
              const stopNorms = (stopNames || []).map(norm).filter(Boolean);
              const stop = heading ? (textNodes
                .filter(node => follows(heading, node))
                .filter(node => stopNorms.includes(norm(node.innerText || node.textContent || "")))
                .sort(domSort)[0] || null) : null;
              const inSection = el => heading && follows(heading, el) && (!stop || before(el, stop));
              const summarize = el => ({
                tag: el.tagName.toLowerCase(),
                text: clean(el.innerText || el.textContent || el.value || el.getAttribute("aria-label") || ""),
                id: el.id || "",
                name: el.getAttribute("name") || "",
                role: el.getAttribute("role") || "",
                automation: el.getAttribute("data-automation-id") || "",
                disabled: !!el.disabled || el.getAttribute("aria-disabled") === "true",
                box: boxOf(el),
                html: clean(el.outerHTML || "").slice(0, 400),
              });
              return {
                scrollY: Math.round(window.scrollY),
                viewport: {width: window.innerWidth, height: window.innerHeight},
                heading: heading ? summarize(heading) : null,
                stop: stop ? summarize(stop) : null,
                buttons: Array.from(document.querySelectorAll("button, [role='button'], a"))
                  .filter(visible)
                  .filter(inSection)
                  .sort(domSort)
                  .slice(0, 20)
                  .map(summarize),
                controls: Array.from(document.querySelectorAll('input:not([type="hidden"]):not([type="file"]), textarea, select, button[aria-haspopup="listbox"], [role="combobox"]'))
                  .filter(visible)
                  .filter(inSection)
                  .sort(domSort)
                  .slice(0, 30)
                  .map(summarize),
              };
            }
        """, {"sectionName": "Education", "stopNames": WORKDAY_EDUCATION_SECTION_STOPS})
    except Exception as err:
        return {"error": str(err)}

def find_workday_section_control(page, section_name, stop_names, selector, label_patterns):
    try:
        handle = page.evaluate_handle(
            workday_section_query_js(),
            {
                "sectionName": section_name,
                "stopNames": stop_names,
                "selector": selector,
                "labelPatterns": label_patterns or [],
            },
        )
        element = handle.as_element()
        return element
    except Exception:
        return None

def fill_workday_section_text_control(page, section_name, stop_names, label_patterns, value):
    if not value:
        return False
    element = find_workday_section_control(
        page,
        section_name,
        stop_names,
        'input:not([type="hidden"]):not([type="file"]), textarea',
        label_patterns,
    )
    if not element:
        return False
    if workday_element_is_search_prompt(element):
        return choose_workday_prompt_input(page, element, value, label_patterns=label_patterns, section_name=section_name, stop_names=stop_names)
    try:
        current = element.evaluate("el => String(el.value || '').trim()")
        if current and normalized_option_text(current) == normalized_option_text(value):
            return True
    except Exception:
        pass
    try:
        element.evaluate("""
            el => {
              el.scrollIntoView({block: "center", inline: "nearest"});
              window.scrollBy(0, -120);
            }
        """)
        page.wait_for_timeout(300)
        element.fill(str(value), timeout=2500)
        page.wait_for_timeout(300)
        return True
    except Exception:
        try:
            element.evaluate("""
                (el, value) => {
                  el.value = value;
                  el.dispatchEvent(new Event("input", {bubbles: true}));
                  el.dispatchEvent(new Event("change", {bubbles: true}));
                }
            """, str(value))
            page.wait_for_timeout(300)
            return True
        except Exception:
            return False

def workday_element_value_contains(element, value):
    if not element or not value:
        return False
    try:
        text = element.evaluate("""
            el => {
              const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
              let chunks = [clean(el.value), clean(el.innerText || el.textContent || ""), clean(el.getAttribute("aria-label"))];
              let node = el.parentElement;
              for (let depth = 0; node && depth < 3; depth++, node = node.parentElement) {
                const text = clean(node.innerText || node.textContent || "");
                if (text && text.length < 800) chunks.push(text);
              }
              return chunks.filter(Boolean).join(" ");
            }
        """)
        return normalized_option_text(value) in normalized_option_text(text)
    except Exception:
        return False

def workday_element_is_search_prompt(element):
    if not element:
        return False
    try:
        return bool(element.evaluate("""
            el => {
              const attrs = [
                el.getAttribute("data-automation-id") || "",
                el.getAttribute("data-uxi-widget-type") || "",
                el.getAttribute("placeholder") || "",
                el.getAttribute("role") || "",
                el.getAttribute("aria-autocomplete") || "",
                el.id || "",
              ].join(" ").toLowerCase();
              return /searchbox|selectinput|search|combobox|autocomplete/.test(attrs);
            }
        """))
    except Exception:
        return False

def choose_workday_prompt_input(
    page,
    element,
    value,
    label_patterns=None,
    section_name=None,
    stop_names=None,
    scroll_attempts=8,
    scroll_amount=420,
    max_options=80,
    visible_timeout=300,
):
    if not element or not value:
        return False
    terms = list(discovery_option_terms(" ".join(label_patterns or []), value))
    value_text = str(value)
    value_norm = normalized_option_text(value_text)
    if value_norm:
        terms.insert(0, rf"^{re.escape(value_text)}$")
    if "university of illinois" in value_norm:
        terms.extend([
            r"University of Illinois at Urbana[- ]Champaign",
            r"University of Illinois Urbana[- ]Champaign",
            r"UIUC",
        ])
    try:
        element.evaluate("""
            el => {
              el.scrollIntoView({block: "center", inline: "nearest"});
              window.scrollBy(0, -100);
            }
        """)
        page.wait_for_timeout(350)
        element.click(timeout=2500, force=True)
        page.wait_for_timeout(250)
        try:
            element.fill("", timeout=1000)
        except Exception:
            page.keyboard.press("Control+A", timeout=600)
            page.keyboard.press("Backspace", timeout=600)
        page.wait_for_timeout(200)
        if "university of illinois" in value_norm:
            element.fill("University of Illinois at Urbana-Champaign", timeout=2500)
        else:
            element.fill(value_text, timeout=2500)
        page.wait_for_timeout(1700)
        if click_workday_option_with_scroll(
            page,
            terms,
            scroll_attempts=scroll_attempts,
            scroll_amount=scroll_amount,
            max_options=max_options,
            visible_timeout=visible_timeout,
        ):
            page.wait_for_timeout(900)
            if section_name and stop_names and workday_section_contains_text(page, section_name, stop_names, value_text):
                return True
            if workday_element_value_contains(element, value_text):
                return True
            return True
    except Exception:
        return False
    return False

def choose_workday_prompt_by_selector(
    page,
    selector,
    values,
    label="",
    require_option=False,
    scroll_attempts=8,
    scroll_amount=420,
    max_options=80,
    visible_timeout=300,
):
    if isinstance(values, str):
        values = [values]
    values = [value for value in values or [] if value]
    if not selector or not values:
        return False
    for value in values:
        for scope in get_apply_scopes(page):
            try:
                locator = scope.locator(selector).first
                if locator.count() == 0 or not locator.is_visible(timeout=700):
                    continue
                if workday_element_value_contains(locator, value):
                    try:
                        page.keyboard.press("Escape", timeout=500)
                    except Exception:
                        pass
                    return True
                if choose_workday_prompt_input(
                    page,
                    locator,
                    value,
                    label_patterns=[label] if label else None,
                    scroll_attempts=scroll_attempts,
                    scroll_amount=scroll_amount,
                    max_options=max_options,
                    visible_timeout=visible_timeout,
                ):
                    try:
                        page.keyboard.press("Escape", timeout=500)
                    except Exception:
                        pass
                    return True
                if require_option:
                    try:
                        clear_workday_element_value(locator)
                    except Exception:
                        pass
                    try:
                        page.keyboard.press("Escape", timeout=500)
                    except Exception:
                        pass
                    continue
                try:
                    locator.click(timeout=1500, force=True)
                    locator.fill(str(value), timeout=2000)
                    page.wait_for_timeout(300)
                    page.keyboard.press("Escape", timeout=500)
                except Exception:
                    pass
                if workday_element_value_contains(locator, value):
                    try:
                        page.keyboard.press("Escape", timeout=500)
                    except Exception:
                        pass
                    return True
                try:
                    page.keyboard.press("Escape", timeout=500)
                except Exception:
                    pass
            except Exception:
                continue
    return False

def choose_workday_section_option(page, section_name, stop_names, label_patterns, value):
    if not value:
        return False
    element = find_workday_section_control(
        page,
        section_name,
        stop_names,
        'select, button[aria-haspopup="listbox"], [role="combobox"], input[role="combobox"], button',
        label_patterns,
    )
    if not element:
        return False
    if workday_element_value_contains(element, value):
        return True
    if workday_element_is_search_prompt(element):
        return choose_workday_prompt_input(page, element, value, label_patterns=label_patterns, section_name=section_name, stop_names=stop_names)
    terms = list(discovery_option_terms(" ".join(label_patterns or []), value))
    if normalized_option_text(value) in US_STATE_ALIASES:
        terms.append(US_STATE_ALIASES[normalized_option_text(value)])
    try:
        tag_name = (element.evaluate("el => el.tagName.toLowerCase()") or "").lower()
        if tag_name == "select":
            options = element.evaluate("""
                el => Array.from(el.options || [])
                  .map(option => ({ value: option.value, text: option.innerText.trim(), disabled: option.disabled }))
            """)
            choice = choose_matching_option(options, " ".join(label_patterns or []), value)
            if choice:
                element.select_option(value=choice["value"], timeout=2000, force=True)
                page.wait_for_timeout(700)
                return workday_element_value_contains(element, value)
    except Exception:
        pass
    try:
        element.evaluate("""
            el => {
              el.scrollIntoView({block: "center", inline: "nearest"});
              window.scrollBy(0, -120);
            }
        """)
        page.wait_for_timeout(350)
        clear_workday_selection_near(element)
        page.wait_for_timeout(250)
        element.click(timeout=2500, force=True)
        page.wait_for_timeout(500)
        try:
            element.fill(str(value), timeout=1000)
            page.wait_for_timeout(500)
        except Exception:
            pass
        if click_workday_option(page, terms):
            page.wait_for_timeout(800)
            return workday_element_value_contains(element, value)
    except Exception:
        return False
    return False

def click_workday_element_center(page, element):
    if not element:
        return False
    for scroll_offset in [220, 140, 60, 0, -80, -160]:
        try:
            element.evaluate("""
                (el, offset) => {
                  el.scrollIntoView({block: "center", inline: "nearest"});
                  window.scrollBy(0, offset);
                }
            """, scroll_offset)
            page.wait_for_timeout(350)
            box = element.bounding_box()
            if not box:
                continue
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.wait_for_timeout(1600)
            return True
        except Exception:
            try:
                element.click(timeout=2000, force=True)
                page.wait_for_timeout(1600)
                return True
            except Exception:
                continue
    try:
        element.evaluate("el => el.click()")
        page.wait_for_timeout(1600)
        return True
    except Exception:
        return False

def activate_workday_section_add(page, element, section_name, stop_names, before_count):
    if not element:
        return False
    actions = ["click", "mouse", "enter", "space", "js", "double"]
    for action in actions:
        try:
            initial_snapshot = {}
            try:
                initial_snapshot = element.evaluate("""
                    el => {
                      const box = el.getBoundingClientRect();
                      return {
                        text: String(el.innerText || el.textContent || el.getAttribute("aria-label") || "").replace(/\\s+/g, " ").trim(),
                        disabled: !!el.disabled || el.getAttribute("aria-disabled") === "true",
                        top: Math.round(box.top),
                        left: Math.round(box.left),
                        width: Math.round(box.width),
                        height: Math.round(box.height),
                        activeBefore: document.activeElement === el,
                      };
                    }
                """)
            except Exception:
                initial_snapshot = {}
            element.evaluate("""
                el => {
                  const box = el.getBoundingClientRect();
                  const targetTop = Math.max(160, Math.min(360, window.innerHeight * 0.42));
                  window.scrollBy(0, box.top - targetTop);
                }
            """)
            page.wait_for_timeout(650)
            if action == "click":
                element.click(timeout=5000)
            elif action == "mouse":
                box = element.bounding_box()
                if not box:
                    continue
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            elif action == "enter":
                element.focus()
                page.keyboard.press("Enter", timeout=1000)
            elif action == "space":
                element.focus()
                page.keyboard.press("Space", timeout=1000)
            elif action == "double":
                box = element.bounding_box()
                if not box:
                    continue
                page.mouse.dblclick(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            else:
                invoked = element.evaluate("""
                    el => {
                      const invokeReact = node => {
                        const event = {
                          type: "click",
                          target: node,
                          currentTarget: node,
                          bubbles: true,
                          cancelable: true,
                          defaultPrevented: false,
                          preventDefault() { this.defaultPrevented = true; },
                          stopPropagation() {},
                          persist() {},
                          nativeEvent: new MouseEvent("click", {bubbles: true, cancelable: true, view: window})
                        };
                        const nodes = [node, ...Array.from(node.querySelectorAll("*"))];
                        let parent = node.parentElement;
                        for (let depth = 0; parent && depth < 6; depth++, parent = parent.parentElement) nodes.push(parent);
                        for (const current of nodes.filter(Boolean)) {
                          const key = Object.keys(current).find(k => k.startsWith("__reactProps$") || k.startsWith("__reactEventHandlers$"));
                          const props = key ? current[key] : null;
                          if (!props) continue;
                          event.currentTarget = current;
                          if (typeof props.onMouseDown === "function") props.onMouseDown(event);
                          if (typeof props.onMouseUp === "function") props.onMouseUp(event);
                          if (typeof props.onClick === "function") {
                            props.onClick(event);
                            return true;
                          }
                        }
                        return false;
                      };
                      if (invokeReact(el)) return "react";
                      el.dispatchEvent(new MouseEvent("mousedown", {bubbles: true, cancelable: true, view: window}));
                      el.dispatchEvent(new MouseEvent("mouseup", {bubbles: true, cancelable: true, view: window}));
                      el.click();
                      return "dom";
                    }
                """)
            deadline = time.time() + 8
            counts = []
            while time.time() < deadline:
                page.wait_for_timeout(500)
                current_count = workday_section_field_count(page, section_name, stop_names)
                counts.append(current_count)
                if current_count > before_count:
                    add_workday_education_add_debug(
                        "section_add_success",
                        section=section_name,
                        action=action,
                        before_count=before_count,
                        counts=counts,
                        element=initial_snapshot,
                    )
                    return True
            add_workday_education_add_debug(
                "section_add_no_change",
                section=section_name,
                action=action,
                before_count=before_count,
                counts=counts,
                element=initial_snapshot,
                body_hint=compact_text(page_body_text(page, timeout=500))[:500],
            )
        except Exception as err:
            add_workday_education_add_debug(
                "section_add_action_exception",
                section=section_name,
                action=action,
                before_count=before_count,
                error=str(err),
            )
            continue
    return False

def click_workday_add_work_experience(page):
    for scope in get_apply_scopes(page):
        try:
            existing = scope.locator('input[id*="workExperience-"][id*="jobTitle"], textarea[id*="workExperience-"], input[name*="workExperience"]')
            for index in range(min(existing.count(), 12)):
                if existing.nth(index).is_visible(timeout=300):
                    return False
        except Exception:
            pass
        for pattern in [
            r"add work experience",
            r"add experience",
            r"add employment",
        ]:
            try:
                locator = scope.get_by_role("button", name=re.compile(pattern, re.IGNORECASE)).first
                if locator.count() and locator.is_visible(timeout=700):
                    locator.click(timeout=3000)
                    page.wait_for_timeout(1200)
                    return True
            except Exception:
                pass
        for xpath in [
            'xpath=//*[contains(normalize-space(.), "Work Experience")]/following::*[self::button or @role="button"][contains(normalize-space(.), "Add")][1]',
            'xpath=//*[contains(normalize-space(.), "Experience")]/following::*[self::button or @role="button"][contains(normalize-space(.), "Add")][1]',
        ]:
            try:
                locator = scope.locator(xpath).first
                if locator.count() and locator.is_visible(timeout=700):
                    locator.scroll_into_view_if_needed(timeout=1000)
                    locator.click(timeout=3000, force=True)
                    page.wait_for_timeout(1200)
                    return True
            except Exception:
                pass
    return False

def workday_education_field_count(scope):
    try:
        return workday_section_field_count(scope.page, "Education", WORKDAY_EDUCATION_SECTION_STOPS)
    except Exception:
        return 0

def click_workday_add_education(page):
    before_count = workday_section_field_count(page, "Education", WORKDAY_EDUCATION_SECTION_STOPS)
    if before_count > 0:
        return False
    try:
        group = page.get_by_role("group", name=re.compile(r"^Education$", re.IGNORECASE)).first
        if group.count() and group.is_visible(timeout=800):
            button = group.get_by_role("button", name=re.compile(r"^Add$", re.IGNORECASE)).first
            if button.count() and button.is_visible(timeout=800):
                add_workday_education_add_debug("try_group_role_add", before_count=before_count)
                if activate_workday_section_add(page, button, "Education", WORKDAY_EDUCATION_SECTION_STOPS, before_count):
                    return True
    except Exception as err:
        add_workday_education_add_debug("group_role_add_exception", error=str(err), before_count=before_count)
    for scope in get_apply_scopes(page):
        try:
            button_handle = scope.evaluate_handle("""
                () => {
                  const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
                  const visible = el => {
                    if (!el || !el.isConnected) return false;
                    const style = window.getComputedStyle(el);
                    const box = el.getBoundingClientRect();
                    return !!(box.width && box.height) && style.visibility !== "hidden" && style.display !== "none";
                  };
                  const norm = value => clean(value).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
                  const follows = (a, b) => !!(a && b && (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING));
                  const before = (a, b) => !!(a && b && (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING));
                  const domSort = (a, b) => {
                    if (a === b) return 0;
                    return follows(a, b) ? -1 : 1;
                  };
                  const nodes = Array.from(document.querySelectorAll("h1,h2,h3,h4,h5,h6,div,span,p"))
                    .filter(visible)
                    .map(node => ({ node, text: clean(node.innerText || node.textContent || "") }))
                    .filter(item => item.text);
                  const education = nodes
                    .filter(item => norm(item.text) === "education")
                    .map(item => item.node)
                    .sort(domSort)
                    .pop();
                  if (!education) return null;
                  const nextStop = nodes
                    .map(item => item.node)
                    .filter(node => follows(education, node))
                    .filter(node => /^(Certifications and Licenses|Certifications|Licenses|Languages|Skills|Resume\\/CV|Resume|CV)$/i.test(clean(node.innerText || node.textContent || "")))
                    .sort(domSort)[0] || null;
                  const inSection = el => follows(education, el) && (!nextStop || before(el, nextStop));
                  const buttons = Array.from(document.querySelectorAll("button, [role='button']"))
                    .filter(visible)
                    .filter(inSection)
                    .map(button => ({ button, text: clean(button.innerText || button.textContent || button.getAttribute("aria-label") || "") }))
                    .filter(item => /\\badd\\b/i.test(item.text) && !/add another/i.test(item.text))
                    .map(item => item.button)
                    .sort(domSort);
                  if (buttons.length) return buttons[0];
                  const fallback = Array.from(document.querySelectorAll("button, [role='button']"))
                    .filter(visible)
                    .filter(inSection)
                    .map(button => ({ button, text: clean(button.innerText || button.textContent || button.getAttribute("aria-label") || "") }))
                    .filter(item => !/save|continue|back|delete|remove|upload/i.test(item.text))
                    .map(item => item.button)
                    .sort(domSort);
                  return fallback.length ? fallback[0] : null;
                }
            """)
            element = button_handle.as_element()
            if element:
                if activate_workday_section_add(page, element, "Education", WORKDAY_EDUCATION_SECTION_STOPS, before_count):
                    return True
        except Exception:
            pass
        try:
            element = find_workday_section_control(
                page,
                "Education",
                WORKDAY_EDUCATION_SECTION_STOPS,
                "button, [role='button']",
                [r"\bAdd\b"],
            )
            if element and activate_workday_section_add(page, element, "Education", WORKDAY_EDUCATION_SECTION_STOPS, before_count):
                return True
        except Exception:
            pass
        for pattern in [
            r"add education",
            r"add school",
            r"add degree",
        ]:
            try:
                locator = scope.get_by_role("button", name=re.compile(pattern, re.IGNORECASE)).first
                if locator.count() and locator.is_visible(timeout=700):
                    locator.evaluate("el => el.scrollIntoView({block: 'center', inline: 'nearest'})")
                    page.wait_for_timeout(500)
                    locator.click(timeout=3000, force=True)
                    page.wait_for_timeout(1800)
                    if workday_section_field_count(page, "Education", WORKDAY_EDUCATION_SECTION_STOPS) > before_count:
                        return True
            except Exception:
                pass
        for xpath in [
            'xpath=//*[contains(normalize-space(.), "Education")]/following::*[self::button or @role="button"][contains(normalize-space(.), "Add")][1]',
            'xpath=//*[contains(normalize-space(.), "School")]/following::*[self::button or @role="button"][contains(normalize-space(.), "Add")][1]',
        ]:
            try:
                locator = scope.locator(xpath).first
                if locator.count() and locator.is_visible(timeout=700):
                    locator.evaluate("el => el.scrollIntoView({block: 'center', inline: 'nearest'})")
                    page.wait_for_timeout(500)
                    locator.click(timeout=3000, force=True)
                    page.wait_for_timeout(1800)
                    if workday_section_field_count(page, "Education", WORKDAY_EDUCATION_SECTION_STOPS) > before_count:
                        return True
            except Exception:
                pass
    return False

def fill_workday_education_from_resume(page, user_data):
    host = (urlparse(page.url or "").hostname or "").lower()
    if "myworkdayjobs.com" not in host:
        return []
    if not is_workday_my_experience_page(page):
        return []
    body_text = page_body_text(page, timeout=1500)
    body_low = body_text.lower()
    if not any(token in body_low for token in ["my experience", "education", "school", "type to add skills"]):
        return []
    education = parse_resume_education(user_data.get("resume_text"), user_data)
    school = education.get("school")
    if not school:
        return []
    write_live_smoke_progress(
        page,
        stage="my_experience",
        action="workday_education_parsed",
        last_field="Education",
        school=school,
        degree=education.get("degree"),
        end_year=education.get("end_year"),
    )
    if (
        workday_education_school_present(page, school)
        and workday_section_contains_text(page, "Education", WORKDAY_EDUCATION_SECTION_STOPS, education.get("end_year"))
    ):
        return []

    clicked_add = False
    if "education" in body_low or "school" in body_low or "type to add skills" in body_low:
        write_live_smoke_progress(page, stage="my_experience", action="workday_education_add_start", last_field="Education Add")
        clicked_add = click_workday_add_education(page)
        page.wait_for_timeout(900)
        write_live_smoke_progress(page, stage="my_experience", action="workday_education_add_done", last_field="Education Add", clicked=clicked_add)

    filled = []
    if clicked_add:
        filled.append({"field": "Education Add", "source": "resume_education", "risk": "medium"})

    write_live_smoke_progress(page, stage="my_experience", action="workday_education_school_text_start", last_field="School")
    school_text_filled = fill_workday_education_school_text_fallback(page, school)
    if school_text_filled:
        filled.append({
            "field": "School",
            "source": "resume_education_text_fallback",
            "risk": "medium",
            "requires_review": True,
            "status": "ok",
        })
    write_live_smoke_progress(
        page,
        stage="my_experience",
        action="workday_education_school_text_done",
        last_field="School",
        filled=school_text_filled,
    )
    if not school_text_filled:
        write_live_smoke_progress(page, stage="my_experience", action="workday_education_school_prompt_start", last_field="School")
        if (
            choose_workday_prompt_by_selector(
                page,
                'input[id*="education-"][id*="schoolName"], input[id*="education-"][id*="school"]',
                workday_education_school_values(school),
                "School",
                require_option=True,
                scroll_attempts=2,
                max_options=30,
                visible_timeout=60,
            )
        ):
            filled.append({"field": "School", "source": "resume_education", "risk": "medium"})
        write_live_smoke_progress(
            page,
            stage="my_experience",
            action="workday_education_school_prompt_done",
            last_field="School",
            present=workday_education_school_present(page, school),
        )
    if not workday_education_school_present(page, school):
        clear_workday_education_school_inputs(page)
        if fill_workday_education_school_text_fallback(page, school):
            filled.append({
                "field": "School",
                "source": "resume_education_text_fallback",
                "risk": "medium",
                "requires_review": True,
                "status": "ok",
            })
        else:
            return [{"field": "Education section not saved", "source": "resume_education", "risk": "medium", "status": "failed"}]
    degree = education.get("degree")
    degree_values = ["Bachelor's Degree", "Bachelors Degree", "Bachelor Degree", "Bachelor of Science"] if degree else []
    degree_filled = False
    write_live_smoke_progress(page, stage="my_experience", action="workday_education_degree_start", last_field="Degree")
    for degree_value in degree_values:
        if (
            choose_workday_control_by_selector(page, 'button[id*="education-"][id*="degree"], [id*="education-"][id*="degree"][aria-haspopup="listbox"]', degree_value, "Degree")
            or choose_workday_section_option(page, "Education", WORKDAY_EDUCATION_SECTION_STOPS, [r"\bDegree\b", r"Education Level", r"Level of Education"], degree_value)
        ):
            degree_filled = True
            break
    if degree_filled:
        filled.append({"field": "Degree", "source": "resume_education", "risk": "medium"})
    write_live_smoke_progress(page, stage="my_experience", action="workday_education_degree_done", last_field="Degree", filled=degree_filled)
    write_live_smoke_progress(page, stage="my_experience", action="workday_education_field_start", last_field="Field of Study")
    if (
        choose_workday_prompt_by_selector(
            page,
            'input[id*="education-"][id*="fieldOfStudy"], input[id*="education-"][id*="field"], input[id*="education-"][id*="major"]',
            [education.get("field"), "Computer Science"],
            "Field of Study",
        )
        or fill_first_visible_locator(page, [
            'input[id*="education-"][id*="field"]:not([data-automation-id="searchBox"])',
            'input[id*="education"][id*="field"]:not([data-automation-id="searchBox"])',
            'input[id*="education-"][id*="major"]:not([data-automation-id="searchBox"])',
            'input[id*="education"][id*="major"]:not([data-automation-id="searchBox"])',
            'input[name*="education"][name*="field"]',
            'input[name*="education"][name*="major"]',
        ], education.get("field"))
    ):
        filled.append({"field": "Field of Study", "source": "resume_education", "risk": "medium"})
    write_live_smoke_progress(page, stage="my_experience", action="workday_education_field_done", last_field="Field of Study")
    if fill_workday_section_text_control(page, "Education", WORKDAY_EDUCATION_SECTION_STOPS, [r"School Location", r"\bLocation\b"], education.get("location")):
        filled.append({"field": "Education Location", "source": "resume_education", "risk": "medium"})

    start_month = education.get("start_month")
    start_year = education.get("start_year")
    end_month = education.get("end_month")
    end_year = education.get("end_year")
    write_live_smoke_progress(page, stage="my_experience", action="workday_education_dates_start", last_field="Education Dates")
    if start_month and choose_workday_section_option(page, "Education", WORKDAY_EDUCATION_SECTION_STOPS, [r"Start Month", r"From Month", r"\bFrom\b"], start_month):
        filled.append({"field": "Education Start Month", "source": "resume_education", "risk": "medium"})
    if start_year and choose_workday_section_option(page, "Education", WORKDAY_EDUCATION_SECTION_STOPS, [r"Start Year", r"From Year", r"\bFrom\b"], start_year):
        filled.append({"field": "Education Start Year", "source": "resume_education", "risk": "medium"})
    if end_month and choose_workday_section_option(page, "Education", WORKDAY_EDUCATION_SECTION_STOPS, [r"End Month", r"To Month", r"Graduation Month", r"Expected Graduation Month", r"\bTo\b"], end_month):
        filled.append({"field": "Education End Month", "source": "resume_education", "risk": "medium"})
    if end_year and choose_workday_section_option(page, "Education", WORKDAY_EDUCATION_SECTION_STOPS, [r"End Year", r"To Year", r"Graduation Year", r"Expected Graduation Year", r"\bTo\b"], end_year):
        filled.append({"field": "Education End Year", "source": "resume_education", "risk": "medium"})
    date_parts = fill_workday_date_parts(
        page,
        "education-",
        {"month_number": "08", "year": start_year},
        {"month_number": "12", "year": end_year},
    )
    for part in date_parts:
        filled.append({"field": f"Education {part}", "source": "resume_education", "risk": "medium"})
    if start_year:
        fill_workday_section_text_control(page, "Education", WORKDAY_EDUCATION_SECTION_STOPS, [r"Start Date", r"From Date"], f"08/{start_year}")
    if end_year:
        if fill_workday_section_text_control(page, "Education", WORKDAY_EDUCATION_SECTION_STOPS, [r"End Date", r"To Date", r"Graduation Date", r"Expected Graduation Date"], f"12/{end_year}"):
            filled.append({"field": "Education End Date", "source": "resume_education", "risk": "medium"})
    if filled and not workday_education_school_present(page, school):
        return [{"field": "Education section not saved", "source": "resume_education", "risk": "medium", "status": "failed"}]
    write_live_smoke_progress(page, stage="my_experience", action="workday_education_dates_done", last_field="Education Dates")
    return filled

def fill_first_visible_locator(page, selectors, value):
    if not value:
        return False
    for scope in get_apply_scopes(page):
        for selector in selectors:
            try:
                locators = scope.locator(selector)
                for index in range(min(locators.count(), 20)):
                    locator = locators.nth(index)
                    if not locator.is_visible(timeout=300):
                        continue
                    current = ""
                    try:
                        current = locator.input_value(timeout=300)
                    except Exception:
                        pass
                    if current and normalized_option_text(current) == normalized_option_text(value):
                        return True
                    locator.fill(str(value), timeout=2000)
                    return True
            except Exception:
                continue
    return False

def fill_workday_segmented_date_input(page, selectors, value):
    if not value:
        return False
    expected = str(value).strip()
    candidate_values = [expected]
    if expected.startswith("0") and len(expected) == 2:
        candidate_values.append(expected[1:])

    def matches(current):
        current = str(current or "").strip()
        if current == expected:
            return True
        if current.isdigit() and expected.isdigit():
            return int(current) == int(expected)
        return False

    for scope in get_apply_scopes(page):
        for selector in selectors:
            try:
                locators = scope.locator(selector)
                for index in range(min(locators.count(), 20)):
                    locator = locators.nth(index)
                    if not locator.is_visible(timeout=300):
                        continue
                    current = ""
                    try:
                        current = locator.input_value(timeout=300).strip()
                    except Exception:
                        pass
                    if matches(current):
                        return True
                    for candidate_value in candidate_values:
                        for method in ("fill", "type", "native"):
                            try:
                                locator.scroll_into_view_if_needed(timeout=800)
                                locator.click(timeout=1500, force=True)
                                page.wait_for_timeout(80)
                                if method == "fill":
                                    locator.fill(candidate_value, timeout=1500)
                                elif method == "type":
                                    locator.press("Control+A", timeout=500)
                                    page.keyboard.type(candidate_value, delay=25)
                                else:
                                    locator.evaluate("""
                                        (el, value) => {
                                          const proto = Object.getPrototypeOf(el);
                                          const desc = Object.getOwnPropertyDescriptor(proto, "value")
                                            || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
                                          if (desc && desc.set) desc.set.call(el, value);
                                          else el.value = value;
                                          el.dispatchEvent(new InputEvent("input", { bubbles: true, data: value, inputType: "insertText" }));
                                          el.dispatchEvent(new Event("change", { bubbles: true }));
                                          el.blur();
                                        }
                                    """, candidate_value)
                                page.wait_for_timeout(150)
                                try:
                                    current = locator.input_value(timeout=300).strip()
                                except Exception:
                                    current = ""
                                if matches(current):
                                    try:
                                        locator.press("Tab", timeout=500)
                                    except Exception:
                                        pass
                                    return True
                            except Exception:
                                continue
            except Exception:
                continue
    return False

def set_workday_segmented_date_by_dom(page, prefix_selector, date_key, segment_key, value):
    if not value:
        return False
    values = [str(value).strip()]
    if values[0].startswith("0") and len(values[0]) == 2:
        values.append(values[0][1:])
    try:
        return bool(page.evaluate("""
            ({prefix, dateKey, segmentKey, values}) => {
              const lower = value => String(value || "").toLowerCase();
              const matchesValue = (current, expected) => {
                current = String(current || "").trim();
                expected = String(expected || "").trim();
                if (current === expected) return true;
                if (/^\\d+$/.test(current) && /^\\d+$/.test(expected)) {
                  return Number(current) === Number(expected);
                }
                return false;
              };
              const visible = el => {
                if (!el || !el.isConnected) return false;
                const style = window.getComputedStyle(el);
                const box = el.getBoundingClientRect();
                return !!(box.width && box.height) && style.visibility !== "hidden" && style.display !== "none";
              };
              const matches = el => {
                const haystack = lower([
                  el.id,
                  el.getAttribute("name"),
                  el.getAttribute("aria-label"),
                  el.getAttribute("data-automation-id")
                ].filter(Boolean).join(" "));
                return haystack.includes(lower(prefix))
                  && haystack.includes(lower(dateKey))
                  && haystack.includes(lower(segmentKey));
              };
              const setNative = (el, next) => {
                const proto = Object.getPrototypeOf(el);
                const desc = Object.getOwnPropertyDescriptor(proto, "value")
                  || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
                if (desc && desc.set) desc.set.call(el, next);
                else el.value = next;
                el.dispatchEvent(new InputEvent("input", { bubbles: true, data: next, inputType: "insertText" }));
                el.dispatchEvent(new Event("change", { bubbles: true }));
                el.blur();
              };
              const target = Array.from(document.querySelectorAll("input"))
                .filter(visible)
                .find(matches);
              if (!target) return false;
              target.scrollIntoView({ block: "center", inline: "nearest" });
              for (const value of values) {
                target.focus();
                setNative(target, String(value));
                if (values.some(expected => matchesValue(target.value, expected))) return true;
              }
              return false;
            }
        """, {
            "prefix": prefix_selector,
            "dateKey": date_key,
            "segmentKey": segment_key,
            "values": values,
        }))
    except Exception:
        return False

def fill_workday_date_parts(page, prefix_selector, start, end):
    filled = []
    start_month = start.get("month_number")
    start_year = start.get("year")
    end_month = end.get("month_number")
    end_year = end.get("year")
    if start_year and fill_workday_segmented_date_input(page, [
        f'input[id*="{prefix_selector}"][id*="startDate"][id*="Year"]',
        f'input[name*="{prefix_selector}"][name*="startDate"][name*="Year"]',
    ], start_year) or set_workday_segmented_date_by_dom(page, prefix_selector, "startDate", "Year", start_year):
        filled.append("start_year")
    if start_month and fill_workday_segmented_date_input(page, [
        f'input[id*="{prefix_selector}"][id*="startDate"][id*="Month"]',
        f'input[name*="{prefix_selector}"][name*="startDate"][name*="Month"]',
    ], start_month) or set_workday_segmented_date_by_dom(page, prefix_selector, "startDate", "Month", start_month):
        filled.append("start_month")
    if end_year and fill_workday_segmented_date_input(page, [
        f'input[id*="{prefix_selector}"][id*="endDate"][id*="Year"]',
        f'input[name*="{prefix_selector}"][name*="endDate"][name*="Year"]',
        f'input[id*="{prefix_selector}"][id*="lastYearAttended"][id*="Year"]',
        f'input[name*="{prefix_selector}"][name*="lastYearAttended"][name*="Year"]',
    ], end_year) or set_workday_segmented_date_by_dom(page, prefix_selector, "endDate", "Year", end_year) or set_workday_segmented_date_by_dom(page, prefix_selector, "lastYearAttended", "Year", end_year):
        filled.append("end_year")
    if end_month and fill_workday_segmented_date_input(page, [
        f'input[id*="{prefix_selector}"][id*="endDate"][id*="Month"]',
        f'input[name*="{prefix_selector}"][name*="endDate"][name*="Month"]',
        f'input[id*="{prefix_selector}"][id*="lastYearAttended"][id*="Month"]',
        f'input[name*="{prefix_selector}"][name*="lastYearAttended"][name*="Month"]',
    ], end_month) or set_workday_segmented_date_by_dom(page, prefix_selector, "endDate", "Month", end_month) or set_workday_segmented_date_by_dom(page, prefix_selector, "lastYearAttended", "Month", end_month):
        filled.append("end_month")
    return filled

def fill_workday_composite_date_question(page, question, user_data):
    if str(getattr(question, "canonical_key", "") or "") != "start_date":
        return {"filled": False, "reason": "not_start_date"}
    raw_value, source_key = profile_start_date_value(user_data)
    parsed = explicit_mmddyyyy_date(raw_value)
    if not parsed:
        return {
            "filled": False,
            "reason": "start_date_missing_or_not_explicit",
            "source_key": source_key,
            "raw_value": raw_value,
        }
    context = getattr(question, "question_context", {}) or {}
    hints = getattr(question, "locator_hints", {}) or {}
    question_text = getattr(question, "raw_text", "") or context.get("nearest_group_text") or ""
    try:
        filled = page.evaluate("""
            ({questionText, hintId, hintName, month, day, year}) => {
              const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
              const norm = value => clean(value).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
              const targetQuestion = norm(questionText);
              const visible = el => {
                if (!el || !el.isConnected) return false;
                if (el.type === "hidden" || el.disabled) return false;
                const style = window.getComputedStyle(el);
                const box = el.getBoundingClientRect();
                return !!(box.width || box.height || el.getClientRects().length) &&
                  style.visibility !== "hidden" && style.display !== "none";
              };
              const labelText = el => {
                const parts = [];
                if (el.id && window.CSS && CSS.escape) {
                  document.querySelectorAll(`label[for="${CSS.escape(el.id)}"]`).forEach(label => parts.push(clean(label.innerText || label.textContent)));
                }
                if (el.getAttribute("aria-labelledby")) {
                  el.getAttribute("aria-labelledby").split(/\\s+/).forEach(id => {
                    const node = document.getElementById(id);
                    if (node) parts.push(clean(node.innerText || node.textContent));
                  });
                }
                parts.push(clean(el.getAttribute("aria-label")));
                parts.push(clean(el.getAttribute("placeholder")));
                parts.push(clean(el.id));
                parts.push(clean(el.name));
                return norm(parts.filter(Boolean).join(" "));
              };
              const setNative = (el, next) => {
                const proto = Object.getPrototypeOf(el);
                const desc = Object.getOwnPropertyDescriptor(proto, "value")
                  || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
                if (desc && desc.set) desc.set.call(el, next);
                else el.value = next;
                el.dispatchEvent(new InputEvent("input", { bubbles: true, data: next, inputType: "insertText" }));
                el.dispatchEvent(new Event("change", { bubbles: true }));
                el.blur();
              };
              const groups = Array.from(document.querySelectorAll("fieldset, [role='group'], [role='radiogroup'], .question, .form-group, [data-question], section, li, div"))
                .filter(visible)
                .filter(group => {
                  const text = norm(group.innerText || group.textContent);
                  if (targetQuestion && text.includes(targetQuestion.slice(0, Math.min(80, targetQuestion.length)))) return true;
                  if (hintId && group.querySelector(`#${CSS.escape(hintId)}`)) return true;
                  if (hintName && group.querySelector(`[name="${CSS.escape(hintName)}"]`)) return true;
                  return false;
                })
                .sort((a, b) => a.querySelectorAll("input").length - b.querySelectorAll("input").length);
              const scopes = groups.length ? groups : [document];
              const result = {};
              for (const scope of scopes) {
                const inputs = Array.from(scope.querySelectorAll("input")).filter(visible);
                const targets = {};
                for (const input of inputs) {
                  const text = labelText(input);
                  if (!targets.month && /(^|\\s)(month|mm)(\\s|$)/.test(text)) targets.month = input;
                  if (!targets.day && /(^|\\s)(day|dd)(\\s|$)/.test(text)) targets.day = input;
                  if (!targets.year && /(^|\\s)(year|yyyy)(\\s|$)/.test(text)) targets.year = input;
                }
                if (targets.month && targets.day && targets.year) {
                  setNative(targets.month, month);
                  setNative(targets.day, day);
                  setNative(targets.year, year);
                  result.month = targets.month.value;
                  result.day = targets.day.value;
                  result.year = targets.year.value;
                  break;
                }
              }
              return result;
            }
        """, {
            "questionText": question_text,
            "hintId": hints.get("id"),
            "hintName": hints.get("name"),
            "month": parsed["month_number"],
            "day": parsed["day"],
            "year": parsed["year"],
        })
    except Exception as err:
        return {"filled": False, "reason": f"start_date_fill_error:{str(err)[:120]}", "source_key": source_key}
    if all(normalized_option_text(filled.get(key)) == normalized_option_text(parsed[key if key != "month" else "month_number"]) for key in ["month", "day", "year"]):
        return {
            "filled": True,
            "source_key": source_key,
            "value": parsed["date"],
            "parts": filled,
        }
    return {
        "filled": False,
        "reason": "start_date_composite_controls_not_found",
        "source_key": source_key,
        "value": parsed["date"],
    }

def cleanup_workday_empty_work_experiences(page):
    removed = 0
    for scope in get_apply_scopes(page):
        try:
            buttons = scope.locator('button, [role="button"]')
            delete_buttons = []
            for index in range(min(buttons.count(), 80)):
                button = buttons.nth(index)
                if not button.is_visible(timeout=200):
                    continue
                label = locator_label(button)
                if re.search(r"\bdelete\b", label or "", re.IGNORECASE):
                    delete_buttons.append(button)
            for button in reversed(delete_buttons):
                should_delete = False
                try:
                    should_delete = bool(button.evaluate("""
                        el => {
                          const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
                          let root = el;
                          for (let depth = 0; root && depth < 8; depth++, root = root.parentElement) {
                            const text = clean(root.innerText || root.textContent || "");
                            if (!/Work Experience\\s+\\d+/i.test(text)) continue;
                            const title = root.querySelector('input[id*="jobTitle"], input[name*="jobTitle"]');
                            const company = root.querySelector('input[id*="companyName"], input[name*="companyName"]');
                            const titleValue = clean(title && title.value);
                            const companyValue = clean(company && company.value);
                            return !titleValue || !companyValue;
                          }
                          return false;
                        }
                    """))
                except Exception:
                    should_delete = False
                if not should_delete:
                    continue
                button.click(timeout=2000, force=True)
                page.wait_for_timeout(800)
                click_matching_control(page, [r"^delete$", r"^confirm$", r"^ok$"], skip_final_submit=True)
                page.wait_for_timeout(1000)
                removed += 1
        except Exception:
            continue
    return removed

def fill_workday_experience_from_resume(page, user_data):
    host = (urlparse(page.url or "").hostname or "").lower()
    if "myworkdayjobs.com" not in host:
        return []
    body_text = page_body_text(page, timeout=1500)
    body_low = body_text.lower()
    if "my experience" not in body_low and "work experience" not in body_low:
        return []
    entries = parse_resume_work_experiences(user_data.get("resume_text"), max_items=1, user_data=user_data)
    if not entries:
        return []
    entry = entries[0]
    removed = cleanup_workday_empty_work_experiences(page)
    page.wait_for_timeout(700 if removed else 100)
    clicked_add = click_workday_add_work_experience(page)
    page.wait_for_timeout(800)

    filled = []
    if removed:
        filled.append({"field": "Work Experience Empty Rows Removed", "count": removed, "source": "resume_experience", "risk": "medium"})
    if clicked_add:
        filled.append({"field": "Work Experience Add", "source": "resume_experience", "risk": "medium"})
    if fill_first_visible_locator(page, ['input[id*="workExperience-"][id*="jobTitle"], input[name*="workExperience"][name*="jobTitle"]'], entry.get("title")) or fill_workday_labeled_any(page, [r"Job Title", r"Title", r"Position"], entry.get("title")):
        filled.append({"field": "Job Title", "source": "resume_experience", "risk": "medium"})
    if fill_first_visible_locator(page, ['input[id*="workExperience-"][id*="companyName"], input[name*="workExperience"][name*="companyName"]'], entry.get("company")) or fill_workday_labeled_any(page, [r"Company", r"Employer", r"Organization"], entry.get("company")):
        filled.append({"field": "Company", "source": "resume_experience", "risk": "medium"})
    if fill_first_visible_locator(page, ['input[id*="workExperience-"][id*="location"], input[name*="workExperience"][name*="location"]'], entry.get("location")) or fill_workday_labeled_any(page, [r"Location"], entry.get("location")):
        filled.append({"field": "Location", "source": "resume_experience", "risk": "medium"})

    start = entry.get("start") or {}
    end = entry.get("end") or {}
    date_parts = fill_workday_date_parts(page, "workExperience-", start, end)
    for part in date_parts:
        filled.append({"field": f"Work Experience {part}", "source": "resume_experience", "risk": "medium"})

    if end.get("present"):
        try:
            if choose_workday_labeled_option(page, r"Currently Work", "Yes"):
                filled.append({"field": "Currently Work Here", "source": "resume_experience", "risk": "medium"})
        except Exception:
            pass

    description = entry.get("description")
    if description and (
        fill_first_visible_locator(page, ['textarea[id*="workExperience-"], textarea[name*="workExperience"]'], description)
        or fill_workday_labeled_any(page, [r"Description", r"Role Description", r"Responsibilities"], description)
    ):
        filled.append({"field": "Description", "source": "resume_experience", "risk": "medium"})
    return filled

WORKDAY_ADAPTER_STATE_KEY = "_workday_adapter_state"

def workday_adapter_v2_enabled():
    return str(os.getenv("WORKDAY_ADAPTER_V2", "0")).strip().lower() in {"1", "true", "yes", "on"}

def reset_workday_adapter_state(user_data):
    if ExecutionState and isinstance(user_data, dict):
        user_data[WORKDAY_ADAPTER_STATE_KEY] = ExecutionState()

def get_workday_adapter_state(user_data):
    if not ExecutionState:
        return None
    if not isinstance(user_data, dict):
        return ExecutionState()
    state = user_data.get(WORKDAY_ADAPTER_STATE_KEY)
    if not isinstance(state, ExecutionState):
        state = ExecutionState()
        user_data[WORKDAY_ADAPTER_STATE_KEY] = state
    return state

def fill_workday_adapter_sections(page, user_data):
    if not workday_adapter_v2_enabled():
        return [], set()
    host = (urlparse(page.url or "").hostname or "").lower()
    if "myworkdayjobs.com" not in host:
        return [], set()
    if WORKDAY_ADAPTER_IMPORT_ERROR:
        return [{
            "field": "Workday adapter unavailable",
            "source": "workday_adapter",
            "risk": "medium",
            "status": "human_required",
            "reason": WORKDAY_ADAPTER_IMPORT_ERROR,
        }], {"education", "experience"}

    state = get_workday_adapter_state(user_data)
    filled = []
    attempted_sections = set()

    if is_workday_my_information_page(page):
        try:
            country_result = CountrySelectorHandler().ensure_united_states(page, state)
            if country_result.ok:
                filled.append({
                    "field": "Country",
                    "source": "workday_adapter.country",
                    "risk": "low",
                    "status": country_result.status,
                    "reason": country_result.reason,
                    "attempts": country_result.attempts,
                })
            else:
                filled.append({
                    "field": "Country",
                    "source": "workday_adapter.country",
                    "risk": "medium",
                    "status": "human_required" if country_result.reason == "country_control_not_found" else country_result.status,
                    "reason": country_result.reason,
                    "attempts": country_result.attempts,
                })
        except Exception as err:
            filled.append({
                "field": "Country",
                "source": "workday_adapter.country",
                "risk": "medium",
                "status": "failed",
                "reason": str(err),
            })

    if is_workday_my_experience_page(page):
        education = parse_resume_education(user_data.get("resume_text"), user_data)
        if education.get("school"):
            attempted_sections.add("education")
            try:
                result = EducationRepeatableSectionHandler().fill(page, education, state)
                filled.append({
                    "field": "Education",
                    "source": "workday_adapter.education",
                    "adapter_section": "education",
                    "risk": "medium",
                    "status": result.status,
                    "reason": result.reason,
                    "attempts": result.attempts,
                })
            except Exception as err:
                filled.append({
                    "field": "Education",
                    "source": "workday_adapter.education",
                    "adapter_section": "education",
                    "risk": "medium",
                    "status": "failed",
                    "reason": str(err),
                })

        entries = parse_resume_work_experiences(user_data.get("resume_text"), max_items=1, user_data=user_data)
        if entries:
            attempted_sections.add("experience")
            try:
                result = ExperienceRepeatableSectionHandler().fill(page, entries[0], state)
                filled.append({
                    "field": "Work Experience",
                    "source": "workday_adapter.experience",
                    "adapter_section": "experience",
                    "risk": "medium",
                    "status": result.status,
                    "reason": result.reason,
                    "attempts": result.attempts,
                })
            except Exception as err:
                filled.append({
                    "field": "Work Experience",
                    "source": "workday_adapter.experience",
                    "adapter_section": "experience",
                    "risk": "medium",
                    "status": "failed",
                    "reason": str(err),
                })
    return filled, attempted_sections

def fill_workday_profile_overrides(page, user_data):
    host = (urlparse(page.url or "").hostname or "").lower()
    if "myworkdayjobs.com" not in host:
        return []
    filled = []
    if is_workday_my_experience_page(page):
        write_live_smoke_progress(page, stage="my_experience", action="workday_my_experience_overrides_start")
        adapter_filled, adapter_attempted_sections = ([], set())
        if workday_adapter_v2_enabled():
            write_live_smoke_progress(page, stage="my_experience", action="workday_adapter_sections_start")
            adapter_filled, adapter_attempted_sections = fill_workday_adapter_sections(page, user_data)
            filled.extend(adapter_filled)
            write_live_smoke_progress(page, stage="my_experience", action="workday_adapter_sections_done")

        if "experience" not in adapter_attempted_sections:
            write_live_smoke_progress(page, stage="my_experience", action="workday_experience_start", last_field="Work Experience")
            experience_filled = fill_workday_experience_from_resume(page, user_data)
            filled.extend(experience_filled)
            write_live_smoke_progress(
                page,
                stage="my_experience",
                action="workday_experience_done",
                last_field="Work Experience",
                filled_count=len(experience_filled),
            )

        if "education" not in adapter_attempted_sections:
            write_live_smoke_progress(page, stage="my_experience", action="workday_education_start", last_field="Education")
            education_filled = fill_workday_education_from_resume(page, user_data)
            filled.extend(education_filled)
            write_live_smoke_progress(
                page,
                stage="my_experience",
                action="workday_education_done",
                last_field="Education",
                filled_count=len(education_filled),
            )

        write_live_smoke_progress(page, stage="my_experience", action="workday_my_experience_overrides_done")
        return filled

    filled.extend(fill_workday_saved_question_answers(page, user_data))
    filled.extend(fill_workday_age_over_18(page, user_data))
    filled.extend(fill_workday_resume_question_answers(page, user_data))
    filled.extend(fill_workday_business_disclosure_no_fields(page, user_data))
    filled.extend(fill_workday_voluntary_disclosure_declines(page, user_data))
    filled.extend(fill_workday_self_identification_signature_fields(page, user_data))
    filled.extend(fill_workday_provisional_select_answers(page, user_data))
    adapter_filled, adapter_attempted_sections = ([], set())
    if workday_adapter_v2_enabled():
        adapter_filled, adapter_attempted_sections = fill_workday_adapter_sections(page, user_data)
    filled.extend(adapter_filled)
    if is_workday_my_experience_page(page):
        if "education" not in adapter_attempted_sections:
            filled.extend(fill_workday_education_from_resume(page, user_data))
        if "experience" not in adapter_attempted_sections:
            filled.extend(fill_workday_experience_from_resume(page, user_data))
    if not workday_adapter_v2_enabled() and force_workday_country_united_states(page):
        filled.append({"field": "Country", "source": "workday_label_override", "risk": "low"})
    if force_workday_phone_country_code_us(page):
        filled.append({"field": "Country Phone Code", "source": "workday_label_override", "risk": "low"})
    if not workday_adapter_v2_enabled() and not workday_country_is_united_states(page) and force_workday_country_united_states(page):
        filled.append({"field": "Country", "source": "workday_final_recheck", "risk": "low"})
    if fill_labeled_text_control(page, "City", user_data.get("city")) or fill_labeled_text_control(page, "Suburb|Locality", user_data.get("city")):
        filled.append({"field": "City/Suburb", "source": "workday_label_override", "risk": "low"})
    state_value = user_data.get("state")
    if workday_country_is_united_states(page):
        if choose_workday_labeled_option(page, "State", state_value):
            filled.append({"field": "State", "source": "workday_label_override", "risk": "low"})
        if choose_workday_control_by_selector(page, 'button#address--countryRegion, [id="address--countryRegion"]', state_value, "State or Territory") or choose_workday_visible_labeled_option(
            page,
            [r"^State or Territory\*?$", r"^State\*?$"],
            state_value,
            avoid_patterns=[r"Phone"],
            after_patterns=[r"^Address$", r"Address Line"],
            before_patterns=[r"^Email Address$", r"^Phone$"],
        ) or choose_workday_near_exact_text_option(
            page,
            [r"^State or Territory\*?$", r"^State\*?$"],
            state_value,
            avoid_patterns=[r"Phone"]
        ) or choose_workday_labeled_option(page, "State or Territory", state_value):
            filled.append({"field": "State or Territory", "source": "workday_label_override", "risk": "low"})
    phone_type = user_data.get("phone_device_type")
    if choose_workday_labeled_option(page, "Phone Device Type", phone_type):
        filled.append({"field": "Phone Device Type", "source": "workday_label_override", "risk": "low"})
    phone_number = normalize_phone_number_value(user_data.get("phone") or user_data.get("phone_number"))
    if fill_labeled_text_control(page, "Phone Number", phone_number):
        filled.append({"field": "Phone Number", "source": "workday_label_override", "risk": "low"})
    if clear_labeled_text_control(page, r"Phone Extension"):
        filled.append({"field": "Phone Extension", "source": "workday_clear_optional_extension", "risk": "low"})
    if is_workday_my_information_page(page):
        page.wait_for_timeout(800)
        if not workday_adapter_v2_enabled() and not workday_country_is_united_states(page) and force_workday_country_united_states(page):
            filled.append({"field": "Country", "source": "workday_final_recheck", "risk": "low"})
        if workday_country_is_united_states(page):
            address_value = user_data.get("address1") or user_data.get("street_address") or user_data.get("address")
            postal_value = user_data.get("postal_code") or user_data.get("zip") or user_data.get("zipcode")
            if fill_labeled_text_control(page, r"Address Line 1|Street Address|Address 1", address_value):
                filled.append({"field": "Address Line 1", "source": "workday_final_recheck", "risk": "medium"})
            if fill_labeled_text_control(page, "City", user_data.get("city")) or fill_labeled_text_control(page, "Suburb|Locality", user_data.get("city")):
                filled.append({"field": "City/Suburb", "source": "workday_final_recheck", "risk": "low"})
            if choose_workday_control_by_selector(page, 'button#address--countryRegion, [id="address--countryRegion"]', state_value, "State or Territory") or choose_workday_visible_labeled_option(
                page,
                [r"^State or Territory\*?$", r"^State\*?$"],
                state_value,
                avoid_patterns=[r"Phone"],
                after_patterns=[r"^Address$", r"Address Line"],
                before_patterns=[r"^Email Address$", r"^Phone$"],
            ) or choose_workday_near_exact_text_option(
                page,
                [r"^State or Territory\*?$", r"^State\*?$"],
                state_value,
                avoid_patterns=[r"Phone"]
            ) or choose_workday_labeled_option(page, "State or Territory", state_value):
                filled.append({"field": "State or Territory", "source": "workday_final_recheck", "risk": "low"})
            if fill_labeled_text_control(page, "Phone Number", phone_number):
                filled.append({"field": "Phone Number", "source": "workday_final_recheck", "risk": "low"})
            if fill_labeled_text_control(page, r"Postal Code|Zip Code|ZIP", postal_value):
                filled.append({"field": "Postal Code", "source": "workday_final_recheck", "risk": "low"})
    return filled

def allowed_visual_field_answers(user_data):
    labels = {
        "first_name": "First name",
        "last_name": "Last name",
        "email": "Email address",
        "phone": "Phone number",
        "phone_country_code": "Country phone code",
        "phone_device_type": "Phone device type",
        "linkedin_url": "LinkedIn URL",
        "address1": "Address line 1",
        "city": "City",
        "state": "State",
        "postal_code": "Postal code",
        "country": "Country",
        "education_school": "Education school",
        "education_degree": "Education degree",
        "education_field": "Education field of study",
        "education_start_month": "Education start month",
        "education_start_year": "Education start year",
        "education_end_month": "Education end / graduation month",
        "education_end_year": "Education end / graduation year",
        "how_heard": "How did you hear about us",
        "previous_worker": "Previously employed by this company",
        "age_over_18": "18 years or older",
        "business_conflict_disclosure": "Government contractor / business relationship / conflict disclosure",
        "authorized_to_work_us": "Authorized to work in the U.S.",
        "need_sponsorship": "Need sponsorship",
        "security_clearance": "Security clearance",
        "start_date": "Start date",
        "notice_period": "Notice period",
        "salary_expectation": "Salary expectation",
        "years_experience": "Years of experience",
    }
    answers = []
    for key, label in labels.items():
        value = user_data.get(key)
        if value in (None, ""):
            continue
        answers.append({"key": key, "label": label, "value": str(value)})
    answers.append({"key": "phone_extension", "label": "Phone extension", "value": ""})
    return answers

def page_has_validation_errors(page):
    text = page_body_text(page, timeout=1200).lower()
    return bool(
        "errors found" in text
        or "is required and must have a value" in text
        or re.search(r"\berror\s*-\s*", text)
    )

def visual_field_fill_step(page, req, user_data, reason="unknown"):
    if not getattr(req, "allow_visual_field_fallback", True):
        return {"acted": False, "reason": "visual_field_fallback_disabled"}
    if not client:
        return {"acted": False, "reason": "vision_client_unavailable"}

    allowed_answers = allowed_visual_field_answers(user_data)
    if not allowed_answers:
        return {"acted": False, "reason": "no_allowed_answers"}

    screenshot_dir = os.path.join(os.path.dirname(__file__), "..", "screenshots")
    os.makedirs(screenshot_dir, exist_ok=True)
    screenshot_bytes = page.screenshot(type="png", full_page=False)
    img_w, img_h = get_png_dimensions(screenshot_bytes)
    screenshot_path = os.path.join(screenshot_dir, f"apply_visual_fields_{int(time.time())}.png")
    try:
        with open(screenshot_path, "wb") as f:
            f.write(screenshot_bytes)
    except Exception:
        screenshot_path = ""

    page_text = page_body_text(page, timeout=1500)[:7000]
    image_part = types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png")
    prompt = f"""
You are a field-level fallback for a job application PRECHECK page.

Current URL: {page.url}
Reason: {reason}
Screenshot resolution: {img_w}x{img_h}.

Page text excerpt:
{page_text}

Allowed answers, and ONLY these answers, are:
{json.dumps(allowed_answers, ensure_ascii=False, indent=2)}

Task:
- Inspect the screenshot and page text for visible required fields, red validation errors, or visibly wrong filled values.
- Return operations only for fields that are visible and can be answered using the allowed answers above.
- For State, prefer the allowed "state" value; if the UI wants the full state name, use the matching full state name.
- For Phone Device Type, use the allowed "phone_device_type" value.
- For City, use the allowed "city" value, not the combined current location.
- For Phone Extension, clear it if it contains the phone number.

Hard safety rules:
- Do NOT click navigation buttons, Save, Continue, Review, Submit, Complete, Finish, or final submission controls.
- Do NOT invent answers.
- Do NOT answer EEO, disability, veteran, gender, race, ethnicity, self-identification, legal certification, or final attestation fields unless a matching explicit allowed answer is provided.
- Return only field fill/select/clear operations.

Return ONLY valid JSON:
{{
  "operations": [
    {{
      "field_label": "visible label such as City or State",
      "answer_key": "one key from allowed answers",
      "value": "exact allowed answer value or full state name if needed",
      "control": "text" | "select" | "radio" | "clear"
    }}
  ],
  "reason": "one concise sentence"
}}
"""
    try:
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=[image_part, prompt],
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0),
        )
        payload = parse_model_json(response.text)
    except Exception as err:
        return {"acted": False, "reason": f"vision_field_query_failed:{err}", "screenshot_path": screenshot_path}

    allowed_by_key = {item["key"]: item["value"] for item in allowed_answers}
    operations = payload.get("operations") or []
    applied = []
    skipped = []
    for op in operations[:8]:
        if not isinstance(op, dict):
            continue
        key = str(op.get("answer_key") or "").strip()
        field_label = str(op.get("field_label") or "").strip()
        control = str(op.get("control") or "text").strip().lower()
        value = "" if op.get("value") is None else str(op.get("value"))
        if key not in allowed_by_key or not field_label:
            skipped.append({"field_label": field_label, "answer_key": key, "reason": "not_allowed"})
            continue
        allowed_value = allowed_by_key.get(key, "")
        value_norm = normalized_option_text(value)
        allowed_norm = normalized_option_text(allowed_value)
        if key != "state" and value_norm != allowed_norm:
            skipped.append({"field_label": field_label, "answer_key": key, "reason": "value_not_allowed"})
            continue
        if key == "state":
            state_norm = allowed_norm
            full_state = US_STATE_ALIASES.get(state_norm, allowed_value)
            if value_norm not in {state_norm, normalized_option_text(full_state), allowed_norm}:
                skipped.append({"field_label": field_label, "answer_key": key, "reason": "state_value_not_allowed"})
                continue
            value = allowed_value
        try:
            if control == "clear":
                ok = clear_labeled_text_control(page, field_label)
            elif control in {"select", "radio"}:
                ok = choose_workday_labeled_option(page, field_label, value)
            else:
                ok = fill_labeled_text_control(page, field_label, value)
                if not ok:
                    ok = choose_workday_labeled_option(page, field_label, value)
            if ok:
                applied.append({"field_label": field_label, "answer_key": key, "control": control})
            else:
                skipped.append({"field_label": field_label, "answer_key": key, "reason": "execution_failed"})
        except Exception as err:
            skipped.append({"field_label": field_label, "answer_key": key, "reason": f"exception:{err}"})

    return {
        "acted": bool(applied),
        "operations": operations,
        "applied": applied,
        "skipped": skipped,
        "reason": payload.get("reason"),
        "screenshot_path": screenshot_path,
    }

def debug_workday_country_state_controls(page):
    debug = []
    for scope_index, scope in enumerate(get_apply_scopes(page)):
        try:
            items = scope.evaluate("""
                () => {
                  const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
                  const visible = el => {
                    if (!el || !el.isConnected) return false;
                    const style = window.getComputedStyle(el);
                    if (style.visibility === "hidden" || style.display === "none") return false;
                    const box = el.getBoundingClientRect();
                    return !!(box.width && box.height);
                  };
                  const textOf = el => clean(
                    el.innerText || el.textContent || el.value || el.getAttribute("aria-label") || ""
                  );
                  const labels = Array.from(document.querySelectorAll("label, span, div, p, legend"))
                    .filter(visible)
                    .map(node => {
                      const box = node.getBoundingClientRect();
                      return { node, text: textOf(node), top: box.top, bottom: box.bottom, left: box.left, right: box.right };
                    })
                    .filter(item => /\\b(country|state|territory|phone code)\\b/i.test(item.text) && item.text.length < 220);
                  const controlSelector = "select,button[aria-haspopup],button[aria-expanded],[role='combobox'],input[role='combobox'],input,button,div[tabindex]";
                  const controls = Array.from(document.querySelectorAll(controlSelector))
                    .filter(visible)
                    .map(control => {
                      const box = control.getBoundingClientRect();
                      const selected = control.tagName.toLowerCase() === "select" && control.selectedIndex >= 0 && control.options
                        ? `${control.options[control.selectedIndex].value} ${control.options[control.selectedIndex].innerText}`
                        : "";
                      return {
                        tag: control.tagName.toLowerCase(),
                        role: control.getAttribute("role") || "",
                        aria: control.getAttribute("aria-label") || control.getAttribute("aria-labelledby") || "",
                        ariaExpanded: control.getAttribute("aria-expanded") || "",
                        ariaHaspopup: control.getAttribute("aria-haspopup") || "",
                        automation: control.getAttribute("data-automation-id") || "",
                        id: control.id || "",
                        name: control.getAttribute("name") || "",
                        value: control.value || "",
                        selected,
                        text: textOf(control).slice(0, 260),
                        top: box.top,
                        bottom: box.bottom,
                        left: box.left,
                        right: box.right,
                        width: box.width,
                        height: box.height
                      };
                    })
                    .filter(control => /country|state|territory|select one|australia|united states|illinois|phone code/i.test(
                      [control.text, control.value, control.selected, control.aria, control.automation, control.id, control.name].join(" ")
                    ));
                  return labels.slice(0, 30).map(label => ({
                    label: label.text,
                    top: label.top,
                    bottom: label.bottom,
                    left: label.left,
                    controls: controls
                      .filter(control => control.top >= label.top - 30 && control.top - label.bottom < 220)
                      .sort((a, b) => Math.abs(a.top - label.bottom) - Math.abs(b.top - label.bottom))
                      .slice(0, 8)
                  }));
                }
            """)
            debug.append({"scope_index": scope_index, "items": items[:30]})
        except Exception as err:
            debug.append({"scope_index": scope_index, "error": str(err)})
    return debug

def is_workday_managed_profile_section_field(page, field):
    host = (urlparse(page.url or "").hostname or "").lower()
    if "myworkdayjobs.com" not in host or not is_workday_my_experience_page(page):
        return False
    text = " ".join(str(field.get(name, "") or "") for name in ["id", "name", "selector", "label"]).lower()
    return bool(re.search(r"\b(education-|workexperience-)", text))

def fill_discovery_page_fields(page, fields, user_data, req):
    global WORKDAY_COUNTRY_SELECTION_DEBUG
    if "myworkdayjobs.com" in (urlparse(page.url or "").hostname or "").lower():
        WORKDAY_COUNTRY_SELECTION_DEBUG = []
        reset_workday_education_add_debug()
    filled = []
    missing_required = []
    skipped = []
    provisional_probe_questions = []
    provisional_probe_metadata = {}
    provisional_probe_filled = []
    satisfied_radio_groups = set()
    allow_placeholders = allow_placeholder_autofill_for_page(page, req)
    if is_workday_my_information_page(page):
        early_profile_filled = []
        if not workday_adapter_v2_enabled() and force_workday_country_united_states(page):
            early_profile_filled.append({"field": "Country", "source": "workday_early_country_recheck", "risk": "low"})
        if force_workday_phone_country_code_us(page):
            early_profile_filled.append({"field": "Country Phone Code", "source": "workday_early_phone_country_code", "risk": "low"})
        if early_profile_filled:
            filled.extend(early_profile_filled)
            try:
                fields = extract_form_schema(page, user_data)
            except Exception:
                pass

    for field in fields:
        if not is_self_identification_decline_field(field):
            continue
        if execution_field_locked_valid(page, field, field.get("label") or "I do not want to answer", user_data):
            skipped.append({
                "field": field_key(field),
                "risk": field.get("risk"),
                "reason": "execution_lock_valid"
            })
            continue
        if discovery_field_is_checked(page, field):
            mark_execution_field_valid(page, field, field.get("label") or "I do not want to answer", user_data, "self_identification_decline")
            continue
        if fill_discovery_field(page, field, field.get("label") or "I do not want to answer", allow_confirmed_sensitive=True):
            mark_execution_field_valid(page, field, field.get("label") or "I do not want to answer", user_data, "self_identification_decline")
            filled.append({
                "field": field_key(field),
                "source": "self_identification_decline",
                "risk": field.get("risk"),
            })

    for field in fields:
        key = field_key(field)
        write_live_smoke_progress(
            page,
            stage=infer_apply_stage(page, fields),
            action="field_preflight",
            last_field=key,
        )
        input_type = field.get("input_type")
        radio_key = radio_group_key(field)
        confirmed_answer = field_answer_override(field.get("label"), user_data)
        profile_answer = candidate_profile_value(field.get("label"), field.get("input_type"), user_data)
        desired_answer = confirmed_answer or profile_answer
        if desired_answer and execution_field_locked_valid(page, field, desired_answer, user_data):
            if radio_key:
                satisfied_radio_groups.add(radio_key)
            skipped.append({
                "field": key,
                "risk": field.get("risk"),
                "reason": "execution_lock_valid"
            })
            continue
        if radio_key and radio_key in satisfied_radio_groups:
            skipped.append({
                "field": key,
                "risk": field.get("risk"),
                "reason": "radio_group_already_satisfied"
            })
            continue
        if field.get("disabled") or field.get("read_only"):
            continue
        if is_workday_managed_profile_section_field(page, field):
            skipped.append({
                "field": key,
                "risk": field.get("risk"),
                "reason": "handled_by_workday_profile_override"
            })
            continue
        if is_protected_workday_country_field(field):
            country_result = fill_protected_workday_country_field(page, field, user_data)
            if country_result.get("status") == "ok":
                expected_country = country_result.get("expected_country") or "United States of America"
                if country_result.get("changed"):
                    filled.append({
                        "field": key,
                        "source": "profile_protected_country",
                        "value": expected_country,
                        "risk": field.get("risk"),
                    })
                else:
                    skipped.append({
                        "field": key,
                        "risk": field.get("risk"),
                        "reason": "already_has_protected_country",
                    })
                mark_execution_field_valid(page, field, expected_country, user_data, "profile_protected_country")
                continue
            missing_required.append({
                "field": key,
                "risk": "medium",
                "reason": "protected_country_mismatch",
                "status": NEEDS_TECHNICAL_REVIEW,
                "blocked_reason": "protected_country_mismatch",
                "expected_country": country_result.get("expected_country") or "United States of America",
                "actual_country": country_result.get("actual_country") or protected_country_visible_value(page, field),
                "stage": "MY_INFORMATION",
            })
            continue
        if input_type in {"checkbox", "radio"} and discovery_field_is_checked(page, field):
            if radio_key:
                satisfied_radio_groups.add(radio_key)
            if desired_answer and answer_matches_field_option(field, desired_answer):
                mark_execution_field_valid(page, field, desired_answer, user_data, "already_checked")
            skipped.append({
                "field": key,
                "risk": field.get("risk"),
                "reason": "field_already_checked"
            })
            continue
        if input_type in {"checkbox", "radio"} and field.get("checked"):
            if desired_answer and not answer_matches_field_option(field, desired_answer):
                skipped.append({
                    "field": key,
                    "risk": field.get("risk"),
                    "reason": "checked_option_does_not_match_desired_answer"
                })
                continue
            if radio_key:
                satisfied_radio_groups.add(radio_key)
            continue
        if input_type not in {"checkbox", "radio"} and field.get("value_present"):
            label_low = (field.get("label") or "").lower()
            if "extension" in label_low and clear_discovery_field(page, field):
                filled.append({
                    "field": field_key(field),
                    "source": "clear_optional_extension",
                    "risk": field.get("risk")
                })
                continue
            current_value = str(field.get("value") or "").strip()
            normalized_current = normalized_option_text(current_value)
            normalized_desired = normalized_option_text(normalize_discovery_value(field.get("label"), input_type, desired_answer))
            if not desired_answer or normalized_current == normalized_desired:
                if desired_answer and normalized_current == normalized_desired:
                    mark_execution_field_valid(page, field, desired_answer, user_data, "already_has_value")
                continue
        if radio_key and radio_group_has_checked(page, field):
            if not desired_answer:
                satisfied_radio_groups.add(radio_key)
                skipped.append({
                    "field": key,
                    "risk": field.get("risk"),
                    "reason": "radio_group_already_checked"
                })
                continue
            if not answer_matches_field_option(field, desired_answer):
                skipped.append({
                    "field": key,
                    "risk": field.get("risk"),
                    "reason": "radio_group_checked_but_this_option_not_desired"
                })
                continue
        if confirmed_answer and req.allow_low_risk_autofill:
            if fill_discovery_field(page, field, confirmed_answer, allow_confirmed_sensitive=True):
                if radio_key:
                    satisfied_radio_groups.add(radio_key)
                mark_execution_field_valid(page, field, confirmed_answer, user_data, "saved_field_answer")
                filled.append({
                    "field": key,
                    "source": "saved_field_answer",
                    "risk": field.get("risk")
                })
                continue

        if field.get("risk") == "high":
            if input_type == "checkbox" and not field.get("required"):
                skipped.append({
                    "field": key,
                    "risk": field.get("risk"),
                    "reason": "optional_high_risk_checkbox_left_unchecked"
                })
                continue
            if input_type in {"checkbox", "radio"} and self_identification_decline_selected(page, field):
                skipped.append({
                    "field": key,
                    "risk": field.get("risk"),
                    "reason": "self_identification_decline_selected"
                })
                continue
            if field.get("required") or input_type in {"radio", "checkbox", "select"}:
                if field_can_defer_to_question_probe(field, req):
                    skipped.append({
                        "field": key,
                        "risk": field.get("risk"),
                        "reason": "deferred_to_question_probe"
                    })
                    continue
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
                mark_execution_field_valid(page, field, "true", user_data, "safe_navigation_checkbox")
                filled.append({
                    "field": key,
                    "source": "safe_navigation_checkbox",
                    "risk": field.get("risk")
                })
                continue

        answer = profile_answer
        source = "profile"
        if not answer and allow_placeholders:
            answer = placeholder_discovery_value(field)
            source = "discovery_placeholder" if answer else None

        if answer and req.allow_low_risk_autofill:
            if fill_discovery_field(page, field, answer, allow_confirmed_sensitive=is_previous_worker_question(field.get("label"))):
                mark_execution_field_valid(page, field, answer, user_data, source)
                filled.append({
                    "field": key,
                    "source": source,
                    "risk": field.get("risk")
                })
                continue

        if req.allow_low_risk_autofill and can_choose_first_valid_required_select(field):
            selected = select_first_valid_option_for_field(page, field)
            if selected:
                selected_text = selected.get("selected") if isinstance(selected, dict) else selected
                mark_execution_field_valid(page, field, selected_text, user_data, "first_valid_select_probe")
                question, metadata = provisional_select_question_from_field(field, selected if isinstance(selected, dict) else {"selected": selected})
                if question:
                    provisional_probe_questions.append(question)
                    provisional_probe_metadata[question.fingerprint] = metadata
                    provisional_probe_filled.append({
                        "fingerprint": question.fingerprint,
                        "field": field_key(field),
                        "probe_answer": selected_text,
                    })
                filled.append({
                    "field": key,
                    "source": "first_valid_select_probe",
                    "value": selected_text,
                    "risk": field.get("risk"),
                    "requires_review": True,
                })
                continue

        if field_requires_user(field, user_data, req, allow_placeholders=allow_placeholders):
            if field_can_defer_to_question_probe(field, req):
                skipped.append({
                    "field": key,
                    "risk": field.get("risk"),
                    "reason": "deferred_to_question_probe"
                })
                continue
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

    workday_overrides = fill_workday_profile_overrides(page, user_data)
    write_live_smoke_progress(
        page,
        stage=infer_apply_stage(page, fields),
        action="workday_profile_overrides_done",
        last_field="workday_profile_overrides",
    )
    filled.extend(workday_overrides)
    for override in workday_overrides:
        if not str(override.get("source") or "").startswith("workday_adapter"):
            continue
        if override.get("status") != "ok":
            missing_required.append({
                "field": override.get("field") or "Workday adapter field",
                "risk": override.get("risk") or "medium",
                "reason": f"workday_adapter_{override.get('status')}",
                "source": override.get("source"),
                "details": override,
            })
    if is_workday_my_information_page(page) and not workday_country_is_united_states(page):
        missing_required.append({
            "field": f"Country currently {workday_selected_country_text(page) or 'unknown'}",
            "risk": "medium",
            "reason": "protected_country_mismatch",
            "status": NEEDS_TECHNICAL_REVIEW,
            "blocked_reason": "protected_country_mismatch",
            "expected_country": "United States of America",
            "actual_country": workday_selected_country_text(page) or "unknown",
            "stage": "MY_INFORMATION",
        })
    if is_workday_my_information_page(page) and not workday_phone_country_code_is_us(page):
        missing_required.append({
            "field": f"Country Phone Code currently {workday_selected_phone_country_code_text(page) or 'unknown'}",
            "risk": "low",
            "reason": "phone_country_code_must_be_united_states",
        })
    if is_workday_my_experience_page(page):
        education = parse_resume_education(user_data.get("resume_text"), user_data)
        school = education.get("school")
        if school and not workday_education_school_present(page, school):
            missing_required.append({
                "field": f"Education missing {school}",
                "risk": "medium",
                "reason": "education_section_not_filled",
            })
    stage = infer_apply_stage(page, fields)
    question_blocker = detect_application_question_blockers(page, fields, user_data, req, stage)
    if provisional_probe_questions:
        provisional_question_blocker = build_question_blocker_outcome(
            page,
            provisional_probe_questions,
            user_data,
            req,
            stage,
            metadata_by_fingerprint=provisional_probe_metadata,
        )
        provisional_question_blocker["probe_filled"] = provisional_probe_filled
        provisional_question_blocker["probe_mode"] = True
        question_blocker = merge_question_blocker_outcomes(question_blocker, provisional_question_blocker)
    if question_blocker and question_blocker.get("blocker_ids") and question_blocker.get("blocking_unprobed_count"):
        missing_required.append({
            "field": "Application question blocker",
            "risk": "high" if question_blocker.get("status") == BLOCKED_ON_QUESTIONS else "medium",
            "reason": "application_question_blocker",
            "status": question_blocker.get("status"),
            "blocker_ids": question_blocker.get("blocker_ids", []),
        })
    is_workday_page = "myworkdayjobs.com" in (urlparse(page.url or "").hostname or "").lower()
    allow_post_structured_visual = not is_workday_page
    if allow_post_structured_visual and (
        page_has_validation_errors(page) or any(
            item.get("field") in {"State", "Phone Device Type", "City"} for item in filled
        )
    ):
        visual_result = visual_field_fill_step(page, req, user_data, reason="post_structured_field_fill")
        if visual_result.get("acted"):
            filled.append({
                "field": "visual_field_fallback",
                "source": "vision",
                "risk": "low",
                "details": visual_result.get("applied")
            })

    result = {
        "filled": filled,
        "missing_required": missing_required,
        "skipped": skipped
    }
    if question_blocker and (question_blocker.get("blocker_ids") or question_blocker.get("trusted_filled") or question_blocker.get("probe_filled")):
        result["question_blocker"] = question_blocker
    if "myworkdayjobs.com" in (urlparse(page.url or "").hostname or "").lower():
        locks = execution_lock_store(user_data)
        result["execution_policy"] = {
            "mode": "deterministic_workday_compiler_v0",
            "visual_field_fallback": "disabled_in_workday_execution_loop",
            "field_locks": len(locks.get("fields", {})),
        }
        result["workday_select_debug"] = debug_workday_country_state_controls(page)
        result["workday_country_selection_debug"] = list(WORKDAY_COUNTRY_SELECTION_DEBUG)
        if is_workday_my_experience_page(page):
            result["workday_education_debug"] = workday_education_debug_snapshot(page)
            result["workday_education_add_debug"] = list(WORKDAY_EDUCATION_ADD_DEBUG)
    return result

def page_supports_bare_final_submit(page):
    text = page_body_text(page, timeout=1200).lower()
    return any(token in text for token in [
        "review your application",
        "review application",
        "application review",
        "application summary",
        "submit application",
        "ready to submit",
        "certify",
        "certification",
        "signature",
        "complete your application",
    ])

def label_is_final_submit(label, allow_bare_submit=False):
    if FINAL_SUBMIT_RE.search(label or ""):
        return True
    return allow_bare_submit and bool(BARE_FINAL_SUBMIT_RE.search(label or ""))

def page_has_final_submit(page):
    allow_bare_submit = page_supports_bare_final_submit(page)
    for scope in get_apply_scopes(page):
        for role in ["button", "link"]:
            try:
                for regex in ([FINAL_SUBMIT_RE, BARE_FINAL_SUBMIT_RE] if allow_bare_submit else [FINAL_SUBMIT_RE]):
                    locator = scope.get_by_role(role, name=regex).first
                    if locator.count() > 0 and locator.is_visible(timeout=500):
                        return True
            except Exception:
                continue
        try:
            controls = scope.locator('button, input[type="submit"], input[type="button"], a')
            for index in range(min(controls.count(), 30)):
                control = controls.nth(index)
                try:
                    if not control.is_visible(timeout=300):
                        continue
                except Exception:
                    continue
                label = locator_label(control)
                if label_is_final_submit(label, allow_bare_submit=allow_bare_submit):
                    return True
        except Exception:
                continue
    return False

def workday_review_has_missing_education(page, user_data):
    host = (urlparse(page.url or "").hostname or "").lower()
    if "myworkdayjobs.com" not in host:
        return False
    text = compact_text(page_body_text(page, timeout=1200))
    if not re.search(r"\bReview\b", text, re.IGNORECASE):
        return False
    school = (user_data or {}).get("education_school") or "University of Illinois"
    if school and normalized_option_text(school) in normalized_option_text(text):
        return False
    return bool(re.search(r"\bEducation\s+No Response\b", text, re.IGNORECASE))

def workday_review_has_bad_highest_education(page):
    host = (urlparse(page.url or "").hostname or "").lower()
    if "myworkdayjobs.com" not in host:
        return False
    text = compact_text(page_body_text(page, timeout=1200))
    if not re.search(r"\bReview\b", text, re.IGNORECASE):
        return False
    return bool(re.search(r"(highest level of education completed|highest education)[\s\S]{0,240}Partial\s+Bachelor", text, re.IGNORECASE))

def click_workday_progress_step(page, label):
    label_norm = normalized_option_text(label)
    try:
        clicked = page.evaluate("""
            (labelNorm) => {
              const clean = value => String(value || "").replace(/\\s+/g, " ").trim();
              const norm = value => clean(value).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
              const visible = node => {
                if (!node || !node.isConnected) return false;
                const style = window.getComputedStyle(node);
                const box = node.getBoundingClientRect();
                return !!(box.width && box.height) && style.visibility !== "hidden" && style.display !== "none";
              };
              const candidates = Array.from(document.querySelectorAll('button, a, [role="button"], li, div, span'))
                .filter(visible)
                .map(node => ({ node, text: clean(node.innerText || node.textContent || node.getAttribute('aria-label') || ''), box: node.getBoundingClientRect() }))
                .filter(item => norm(item.text) === labelNorm && item.box.top < 360)
                .sort((a, b) => a.box.top - b.box.top || a.box.left - b.box.left);
              const target = candidates[0] && candidates[0].node;
              if (!target) return false;
              target.click();
              return true;
            }
        """, label_norm)
        if clicked:
            page.wait_for_timeout(3000)
            wait_for_apply_page_ready(page, timeout=25000, allow_reload=False)
            return True
    except Exception:
        pass
    try:
        locator = page.get_by_text(re.compile(rf"^{re.escape(label)}$", re.IGNORECASE)).first
        if locator.count() and locator.is_visible(timeout=800):
            locator.click(timeout=3000, force=True)
            page.wait_for_timeout(3000)
            wait_for_apply_page_ready(page, timeout=25000, allow_reload=False)
            return True
    except Exception:
        pass
    return False

def is_application_flow_stage(stage):
    return stage in {
        "application_form",
        "my_information",
        "my_experience",
        "application_questions",
        "voluntary_disclosures",
        "self_identify",
        "review",
    }

def click_final_submit_control(page):
    patterns = [
        FINAL_SUBMIT_RE.pattern,
        r"\bsubmit application\b",
        r"\bsend application\b",
        r"\bcomplete application\b",
        r"\bfinish application\b",
    ]
    if page_supports_bare_final_submit(page):
        patterns.extend([r"^\s*submit\s*$", r"^\s*send\s*$", r"^\s*complete\s*$", r"^\s*finish\s*$"])
    return click_matching_control(page, patterns, skip_final_submit=False, avoid_patterns=[
        r"back",
        r"previous",
        r"cancel",
        r"delete",
        r"withdraw",
        r"save",
        r"draft",
        r"edit",
    ])

def submission_success_detected(page):
    text = page_body_text(page, timeout=2500)
    if not text:
        return False
    return bool(SUBMISSION_SUCCESS_RE.search(text))

def capture_apply_screenshot(page, prefix):
    screenshot_dir = os.path.join(os.path.dirname(__file__), "..", "screenshots")
    os.makedirs(screenshot_dir, exist_ok=True)
    screenshot_path = os.path.join(screenshot_dir, f"{prefix}_{int(time.time())}.png")
    try:
        page.screenshot(path=screenshot_path, full_page=True)
    except Exception as screenshot_err:
        print(f"[Apply Submit] Screenshot failed: {screenshot_err}")
        screenshot_path = ""
    return screenshot_path

def non_final_blocking_issues(preflight):
    return [
        item for item in (preflight or {}).get("blocking_issues", [])
        if item.get("type") != "final_submit_guard"
    ]

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

def apply_page_signature(page, fields):
    base = f"{page.url}\n{schema_signature(fields)}"
    host = (urlparse(page.url or "").hostname or "").lower()
    if "workdayjobs.com" not in host and "myworkdaysite.com" not in host:
        return base
    body = compact_text(page_body_text(page, timeout=1000))
    heading = ""
    heading_match = re.search(
        r"\b(My Information|My Experience|Application Questions\s+\d+\s+of\s+\d+|Voluntary Disclosures|Self Identify|Review)\b",
        body,
        re.IGNORECASE,
    )
    if heading_match:
        heading = heading_match.group(0)
    body_hash = hashlib.sha1(body[:5000].encode("utf-8", errors="ignore")).hexdigest()[:16] if body else ""
    return f"{base}\n{infer_apply_stage(page, fields)}\n{heading}\n{body_hash}"

def application_request_id(req, user_data):
    return (
        getattr(req, "application_id", None)
        or (user_data or {}).get("application_id")
        or (user_data or {}).get("app_id")
    )

def application_batch_id(req, user_data):
    return getattr(req, "batch_id", None) or (user_data or {}).get("batch_id")

def approved_question_answers_from_user_data(user_data):
    data = user_data or {}
    return {entry["key"]: entry["answer"] for entry in trusted_question_library_entries(data)}


def trusted_question_library_entries(user_data):
    data = user_data or {}
    entries = []
    seen = set()

    def add_entry(key, answer, source):
        if key in (None, "") or answer in (None, ""):
            return
        key_text = str(key)
        dedupe_key = (source, key_text, json.dumps(answer, sort_keys=True, default=str))
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        entries.append({
            "key": key_text,
            "answer": answer,
            "source": source,
        })

    for key in ["approved_question_answers", "approved_answers", "question_blocker_answers"]:
        value = data.get(key)
        if isinstance(value, dict):
            for answer_key, answer_value in value.items():
                add_entry(answer_key, answer_value, key)
    question_bank = data.get("question_bank", []) or []
    if isinstance(question_bank, dict):
        for answer_key, answer_value in question_bank.items():
            add_entry(answer_key, answer_value, "question_bank")
    else:
        for item in question_bank:
            if not isinstance(item, dict):
                continue
            question_key = item.get("fingerprint") or item.get("normalized_text") or item.get("field") or item.get("question")
            add_entry(question_key, item.get("value"), "question_bank")
    for library_key in ["common_answers", "profile_library", "profile"]:
        library = data.get(library_key)
        if not isinstance(library, dict):
            continue
        for key, value in library.items():
            if isinstance(value, dict):
                answer_value = value.get("value")
            else:
                answer_value = value
            add_entry(key, answer_value, library_key)
    for key in TRUSTED_QUESTION_ALIAS_PATTERNS:
        add_entry(key, data.get(key), "profile")
    return entries

def persist_question_blocker_to_memory(application_id, payload):
    if not application_id:
        return {"created": False, "blocker": payload, "memory_persisted": False}
    try:
        import requests
        response = requests.post(
            mongo_service_url(f"/applications/{application_id}/question-blockers"),
            json=payload,
            timeout=4,
        )
        if response.status_code >= 400:
            return {
                "created": False,
                "blocker": payload,
                "memory_persisted": False,
                "memory_error": f"HTTP {response.status_code}: {response.text[:240]}",
            }
        result = response.json()
        result["memory_persisted"] = True
        return result
    except Exception as err:
        return {
            "created": False,
            "blocker": payload,
            "memory_persisted": False,
            "memory_error": str(err)[:240],
        }

def build_question_blocker_outcome(page, questions, user_data, req, stage, metadata_by_fingerprint=None):
    app_id = application_request_id(req, user_data) or f"local-{hashlib.sha256((page.url or stage or 'application').encode('utf-8')).hexdigest()[:12]}"
    batch_id = application_batch_id(req, user_data)
    status = outcome_status_for_questions(questions) if outcome_status_for_questions else BLOCKED_ON_QUESTIONS
    persisted = []
    blocker_ids = []
    metadata_by_fingerprint = metadata_by_fingerprint or {}
    host = (urlparse(page.url or "").hostname or "").lower()
    tenant = host.split(".")[0] if host else None
    for question in questions:
        metadata = dict(metadata_by_fingerprint.get(question.fingerprint) or {})
        payload = {
            "batch_id": batch_id,
            "ats": infer_ats(page.url or ""),
            "tenant": tenant,
            "company": (user_data or {}).get("company"),
            "role": (user_data or {}).get("role"),
            "job_url": getattr(req, "url", None) or page.url,
            "page_name": stage,
            "stage": stage or "unknown",
            "raw_text": question.raw_text,
            "normalized_text": question.normalized_text,
            "fingerprint": question.fingerprint,
            "canonical_key": question.canonical_key,
            "required": question.required,
            "control_type": question.control_type,
            "options": question.options,
            "validation_message": question.validation_message,
            "locator_hints": question.locator_hints,
            "artifacts": [],
            "metadata": metadata,
            "status": TECHNICAL_REVIEW if question.status == TECHNICAL_REVIEW else UNANSWERED,
            "approved_answer": None,
        }
        result = persist_question_blocker_to_memory(application_request_id(req, user_data), payload)
        blocker = result.get("blocker") or payload
        blocker_id = blocker.get("id") or hashlib.sha256(
            f"{app_id}|{stage}|{question.fingerprint}".encode("utf-8")
        ).hexdigest()[:32]
        blocker_ids.append(blocker_id)
        persisted.append({**result, "id": blocker_id, "fingerprint": question.fingerprint})
    return {
        "status": status,
        "application_id": app_id,
        "batch_id": batch_id,
        "blocker_ids": blocker_ids,
        "questions": detector_questions_payload(questions) if detector_questions_payload else [],
        "checkpoint": {
            "ats": infer_ats(page.url or ""),
            "stage": stage,
            "url": page.url,
        },
        "memory_results": persisted,
        "probe_filled_count": sum(1 for item in metadata_by_fingerprint.values() if item.get("probe_answer_applied")),
        "blocking_unprobed_count": sum(
            1
            for question in questions
            if not (metadata_by_fingerprint.get(question.fingerprint) or {}).get("probe_answer_applied")
        ),
        "requires_final_review": any(item.get("requires_user_review") for item in metadata_by_fingerprint.values()),
    }

def merge_question_blocker_outcomes(left, right):
    if not left:
        return right
    if not right:
        return left
    merged = dict(left)
    if left.get("status") == NEEDS_TECHNICAL_REVIEW or right.get("status") == NEEDS_TECHNICAL_REVIEW:
        merged["status"] = NEEDS_TECHNICAL_REVIEW
    elif left.get("status") or right.get("status"):
        merged["status"] = left.get("status") or right.get("status")

    def marker_for(item, item_key):
        if isinstance(item, dict):
            return item.get(item_key) or item.get("fingerprint") or item.get("id") or json.dumps(item, sort_keys=True, default=str)
        return item

    def extend_unique(key, item_key="fingerprint"):
        seen = set()
        values = []
        for source in (left, right):
            for item in source.get(key) or []:
                marker = marker_for(item, item_key)
                if marker in seen:
                    continue
                seen.add(marker)
                values.append(item)
        if values:
            merged[key] = values

    extend_unique("blocker_ids", "id")
    extend_unique("questions", "fingerprint")
    extend_unique("memory_results", "id")
    extend_unique("trusted_filled", "fingerprint")
    extend_unique("probe_filled", "fingerprint")
    merged["requires_final_review"] = bool(left.get("requires_final_review") or right.get("requires_final_review"))
    merged["probe_mode"] = bool(left.get("probe_mode") or right.get("probe_mode"))
    merged["probe_filled_count"] = len(merged.get("probe_filled") or [])
    merged["blocking_unprobed_count"] = int(left.get("blocking_unprobed_count") or 0) + int(right.get("blocking_unprobed_count") or 0)
    return merged

def trusted_answer_match_result(matched=False, answer=None, source_key="", source="", confidence=0.0, match_method="", reason="", requires_review=False):
    return {
        "matched": bool(matched),
        "answer": answer,
        "source_key": source_key or "",
        "source": source or "",
        "confidence": float(confidence or 0.0),
        "match_method": match_method or "",
        "reason": reason or "",
        "requires_review": bool(requires_review),
    }


def answers_equivalent(left, right):
    try:
        if left == right:
            return True
    except Exception:
        pass
    return normalized_option_text(left) == normalized_option_text(right)


def user_data_config_value(user_data, key, default=None):
    data = user_data if isinstance(user_data, dict) else {}
    for source in [data.get("question_blocker_config"), data.get("application_question_config"), data]:
        if isinstance(source, dict) and key in source:
            return source.get(key)
    env_key = key.upper()
    if env_key in os.environ:
        return os.environ.get(env_key)
    return default


def config_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def llm_library_matcher_allowed(question, user_data):
    if not config_bool(user_data_config_value(user_data, "enable_llm_library_matcher"), default=False):
        return False
    text = " ".join([
        str(question.raw_text or ""),
        str(question.normalized_text or ""),
        str(getattr(question, "canonical_key", "") or ""),
    ])
    if is_sensitive_question(text) and not config_bool(
        user_data_config_value(user_data, "enable_llm_library_matcher_for_sensitive_questions"),
        default=False,
    ):
        return False
    return True


def llm_library_match_cache(user_data):
    if not isinstance(user_data, dict):
        return {}
    cache = user_data.setdefault("_llm_library_match_cache", {})
    return cache if isinstance(cache, dict) else {}


def llm_match_call_budget_available(user_data):
    if not isinstance(user_data, dict):
        return True
    max_calls = user_data_config_value(user_data, "max_llm_match_calls_per_application")
    if max_calls in (None, ""):
        return True
    try:
        limit = int(max_calls)
    except Exception:
        return True
    used = int(user_data.get("_llm_library_match_calls", 0) or 0)
    return used < max(0, limit)


def llm_match_trusted_answer(question, trusted_entries, options=None):
    if not client or not trusted_entries:
        return None
    safe_entries = [
        {
            "key": str(entry.get("key") or "")[:160],
            "answer": str(entry.get("answer") or "")[:240],
            "source": str(entry.get("source") or "")[:80],
        }
        for entry in trusted_entries[:80]
        if entry.get("key") and entry.get("answer") not in (None, "")
    ]
    if not safe_entries:
        return None
    prompt = f"""
You are a matcher for job application required questions.

You must ONLY select an answer that already exists in the trusted library entries.
Do not invent, infer, transform, or generate answers.
If uncertain, return matched_library_key=null.

ATS question:
{json.dumps({
    "text": question.raw_text or question.normalized_text,
    "canonical_key": getattr(question, "canonical_key", None),
    "control_type": question.control_type,
    "options": options or question.options or [],
    "question_context": getattr(question, "question_context", {}) or {},
}, ensure_ascii=False)}

Trusted library entries:
{json.dumps(safe_entries, ensure_ascii=False, indent=2)}

Return ONLY strict JSON:
{{
  "matched_library_key": string | null,
  "answer": existing library answer | null,
  "confidence": number,
  "reason": string,
  "safe_to_autofill": boolean
}}
"""
    try:
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0),
        )
        return parse_model_json(response.text)
    except Exception as err:
        return {
            "matched_library_key": None,
            "answer": None,
            "confidence": 0.0,
            "reason": f"llm_match_failed:{str(err)[:120]}",
            "safe_to_autofill": False,
        }

def llm_classify_question_context(context, user_data=None):
    if not client:
        return None
    allowed_keys = {
        "authorized_to_work_us",
        "need_sponsorship",
        "conflict_of_interest",
        "export_control",
        "current_or_previous_company_employee",
        "start_date",
    }
    safe_context = {
        key: context.get(key)
        for key in [
            "aria_labelledby_text",
            "label_for_text",
            "fieldset_legend",
            "nearest_group_text",
            "validation_message",
            "preceding_sibling_text",
            "options",
            "current_stage",
        ]
        if context.get(key) not in (None, "")
    }
    prompt = f"""
You classify ATS question context before answer matching.

You may recover the real question text and choose one canonical_key.
You must not choose, infer, or suggest an answer.

Allowed canonical_key values:
{json.dumps(sorted(allowed_keys))}

Question context:
{json.dumps(safe_context, ensure_ascii=False, indent=2)[:5000]}

Return ONLY strict JSON:
{{
  "question_text": string | null,
  "canonical_key": string | null,
  "confidence": number,
  "evidence": string
}}
"""
    try:
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0),
        )
        result = parse_model_json(response.text)
    except Exception:
        return None
    if not isinstance(result, dict):
        return None
    canonical_key = result.get("canonical_key")
    if canonical_key and canonical_key not in allowed_keys:
        result["canonical_key"] = None
    result.pop("answer", None)
    result.pop("safe_to_autofill", None)
    return result


def match_question_to_trusted_answer(question, user_data, options=None):
    entries = trusted_question_library_entries(user_data)
    entries_by_key = {entry["key"]: entry for entry in entries if entry.get("key")}
    for key in [question.fingerprint, question.normalized_text, question.raw_text, question.canonical_key]:
        entry = entries_by_key.get(key) if key else None
        if entry and entry.get("answer") not in (None, ""):
            return trusted_answer_match_result(
                True,
                entry["answer"],
                entry["key"],
                entry["source"],
                1.0,
                "exact",
                "Matched exact question key.",
                False,
            )
    question_text = " ".join([
        str(question.raw_text or ""),
        str(question.normalized_text or ""),
        str(question.canonical_key or ""),
        str((getattr(question, "question_context", {}) or {}).get("nearest_group_text") or ""),
        str((getattr(question, "question_context", {}) or {}).get("validation_message") or ""),
    ])
    for entry in entries:
        value = entry.get("answer")
        if value in (None, ""):
            continue
        key_norm = normalized_option_text(entry.get("key")).replace(" ", "_")
        patterns = TRUSTED_QUESTION_ALIAS_PATTERNS.get(key_norm)
        if patterns and any(re.search(pattern, question_text, re.IGNORECASE) for pattern in patterns):
            return trusted_answer_match_result(
                True,
                value,
                entry.get("key"),
                entry.get("source"),
                0.95,
                "alias",
                f"Matched deterministic trusted-answer alias '{entry.get('key')}'.",
                False,
            )

    if not llm_library_matcher_allowed(question, user_data):
        return trusted_answer_match_result(False, reason="No trusted answer matched.")
    cache = llm_library_match_cache(user_data)
    cache_key = question.fingerprint or question.normalized_text or question.raw_text
    if cache_key in cache:
        llm_result = cache.get(cache_key)
    elif not llm_match_call_budget_available(user_data):
        return trusted_answer_match_result(False, reason="LLM matcher call budget exhausted.")
    else:
        if isinstance(user_data, dict):
            user_data["_llm_library_match_calls"] = int(user_data.get("_llm_library_match_calls", 0) or 0) + 1
        llm_result = llm_match_trusted_answer(question, entries, options=options)
        if cache_key:
            cache[cache_key] = llm_result
    if not isinstance(llm_result, dict):
        return trusted_answer_match_result(False, reason="No trusted answer matched.")
    matched_key = llm_result.get("matched_library_key")
    entry = entries_by_key.get(matched_key) if matched_key else None
    confidence = float(llm_result.get("confidence") or 0.0)
    answer = llm_result.get("answer")
    if not entry or answer in (None, "") or not answers_equivalent(answer, entry.get("answer")):
        return trusted_answer_match_result(
            False,
            reason="LLM matcher did not select an existing trusted library answer.",
        )
    if confidence < 0.70 or not llm_result.get("safe_to_autofill", False):
        return trusted_answer_match_result(
            False,
            answer=entry.get("answer"),
            source_key=entry.get("key"),
            source=entry.get("source"),
            confidence=confidence,
            match_method="llm",
            reason=llm_result.get("reason") or "LLM match confidence below review threshold.",
        )
    return trusted_answer_match_result(
        True,
        entry.get("answer"),
        entry.get("key"),
        entry.get("source"),
        confidence,
        "llm",
        llm_result.get("reason") or "LLM selected an existing trusted library answer.",
        confidence < 0.90,
    )


def trusted_answer_for_detected_question(question, user_data):
    match = match_question_to_trusted_answer(question, user_data)
    return match.get("answer") if match.get("matched") and not match.get("requires_review") else None


def trusted_answer_match_metadata(match, prefix="answer"):
    metadata = {
        f"{prefix}_source_key": match.get("source_key"),
        f"{prefix}_source": match.get("source"),
        f"{prefix}_match_method": match.get("match_method"),
        f"{prefix}_match_confidence": match.get("confidence"),
        f"{prefix}_match_reason": match.get("reason"),
    }
    return {key: value for key, value in metadata.items() if value not in (None, "")}


def map_trusted_answer_to_question_option(question, answer):
    control_type = str(question.control_type or "").lower()
    if control_type not in {"select", "radio"}:
        return answer
    options = [str(option or "").strip() for option in (question.options or []) if str(option or "").strip()]
    if not options:
        return None
    answer_text = "Yes" if answer is True else ("No" if answer is False else str(answer or "").strip())
    answer_norm = normalized_option_text(answer_text)
    if not answer_norm:
        return None
    placeholders = re.compile(r"^(select|choose)( one| an option| an answer| answer)?$", re.IGNORECASE)
    for option in options:
        if placeholders.search(option):
            continue
        option_norm = normalized_option_text(option)
        if option_norm == answer_norm:
            return option
    for option in options:
        if placeholders.search(option):
            continue
        option_norm = normalized_option_text(option)
        if answer_norm and (answer_norm in option_norm or option_norm in answer_norm):
            return option
    return None


def question_text_matches_candidate(question_text, candidate_text):
    question_norm = normalized_option_text(question_text)
    candidate_norm = normalized_option_text(candidate_text)
    if not question_norm or not candidate_norm:
        return False
    return question_norm in candidate_norm or candidate_norm in question_norm


def field_group_matches_question(field, question):
    question_text = question.raw_text or question.normalized_text
    for key in ["label", "raw_label", "group_label", "group_text"]:
        if question_text_matches_candidate(question_text, field.get(key)):
            return True
    hints = question.locator_hints or {}
    hint_name = normalized_option_text(hints.get("name"))
    if hint_name and hint_name == normalized_option_text(field.get("name")):
        return True
    for hint_key, field_key_name in [
        ("id", "id"),
        ("data_question", "data_question"),
        ("data_field", "data_field"),
    ]:
        hint_value = normalized_option_text(hints.get(hint_key))
        field_value = normalized_option_text(field.get(field_key_name))
        if hint_value and field_value and hint_value == field_value:
            return True
    return False


def field_matches_detected_question(field, question):
    control_type = str(question.control_type or "").lower()
    if control_type in {"radio", "checkbox"}:
        return field_group_matches_question(field, question)
    field_text = " ".join(str(field.get(key, "")) for key in [
        "label",
        "raw_label",
        "group_label",
        "group_text",
        "name",
        "id",
        "placeholder",
        "data_question",
        "data_field",
    ])
    return question_text_matches_candidate(question.raw_text or question.normalized_text, field_text)


def field_has_scoped_validation_error(page, field):
    scope = get_scope_by_index(page, field.get("scope_index"))
    selector = field.get("selector")
    if not selector:
        return False
    try:
        locator = scope.locator(selector).first
        if locator.count() == 0:
            return False
        return bool(locator.evaluate(
            """
            (el) => {
              const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
              const visible = node => {
                if (!node || !node.isConnected) return false;
                const style = window.getComputedStyle(node);
                const box = node.getBoundingClientRect();
                return !!(box.width || box.height || node.getClientRects().length) &&
                  style.visibility !== 'hidden' && style.display !== 'none';
              };
              if (el.getAttribute('aria-invalid') === 'true') return true;
              const describedBy = clean(el.getAttribute('aria-describedby'));
              if (describedBy) {
                for (const id of describedBy.split(/\\s+/)) {
                  const node = document.getElementById(id);
                  if (visible(node) && /error|required|invalid/i.test(clean(node.innerText || node.textContent))) return true;
                }
              }
              const container = el.closest('fieldset, [role="group"], [role="radiogroup"], .form-group, .field, .question, [data-question], [data-field-container]') || el.parentElement;
              if (!container) return false;
              for (const node of container.querySelectorAll('[role="alert"], .error, .validation-error, [data-validation], [data-error]')) {
                if (visible(node) && /error|required|invalid/i.test(clean(node.innerText || node.textContent))) return true;
              }
              return false;
            }
            """
        ))
    except Exception:
        return False


def probe_stop_reason_for_question(question):
    text = " ".join([
        str(question.raw_text or ""),
        str(question.normalized_text or ""),
        str(getattr(question, "canonical_key", "") or ""),
        str(question.validation_message or ""),
        " ".join(str(option or "") for option in (question.options or [])),
    ])
    control_type = str(question.control_type or "").lower()
    if control_type == "file":
        return "required_file_input"
    if PROBE_IMMEDIATE_STOP_RE.search(text):
        return "probe_unsafe_attestation_or_signature"
    if getattr(question, "canonical_key", None) in {
        "authorized_to_work_us",
        "need_sponsorship",
        "conflict_of_interest",
        "export_control",
        "current_or_previous_company_employee",
    }:
        return "sensitive_question_requires_trusted_answer"
    if is_sensitive_question(text):
        return "sensitive_question_requires_trusted_answer"
    if control_type not in PROBE_SUPPORTED_CONTROL_TYPES:
        return f"unsupported_probe_control_{control_type or 'unknown'}"
    return None


def first_probe_option(options):
    for option in options or []:
        text = str(option or "").strip()
        if text and not re.search(r"^(select|choose)( one| an option| an answer| answer)?$", text, re.IGNORECASE):
            return text
    return None


def probe_value_for_question(question):
    control_type = str(question.control_type or "").lower()
    if control_type in {"select", "radio"}:
        return first_probe_option(question.options)
    if control_type == "checkbox":
        text = f"{question.raw_text} {' '.join(question.options or [])}"
        if SAFE_DISCOVERY_CHECKBOX_RE.search(text) and not PROBE_IMMEDIATE_STOP_RE.search(text):
            return "true"
        return None
    if control_type in {"text", "textarea"}:
        return PROBE_TEXT_PLACEHOLDER
    return None


def fill_detected_question_value(page, question, fields, value, allow_confirmed_sensitive=False):
    control_type = str(question.control_type or "").lower()
    candidates = [field for field in fields if not field.get("disabled") and not field.get("read_only") and field_matches_detected_question(field, question)]
    if control_type == "radio":
        candidates = [field for field in candidates if field.get("input_type") == "radio" and answer_matches_field_option(field, value)]
    elif control_type == "checkbox":
        candidates = [field for field in candidates if field.get("input_type") == "checkbox"]
    elif control_type == "select":
        candidates = [field for field in candidates if field.get("input_type") in {"select", "text", "input"} or field.get("tag") == "select"]
    elif control_type == "textarea":
        candidates = [field for field in candidates if field.get("input_type") == "textarea" or field.get("tag") == "textarea"]
    elif control_type == "text":
        candidates = [field for field in candidates if field.get("input_type") in {"text", "input", "search"}]
    for field in candidates:
        if fill_discovery_field(page, field, value, allow_confirmed_sensitive=allow_confirmed_sensitive):
            return field
    return None


def read_visible_workday_options_for_field(page, field, max_options=40):
    selector = field.get("selector")
    if not selector:
        return []
    scope = get_scope_by_index(page, field.get("scope_index"))
    try:
        control = scope.locator(selector).first
        if control.count() == 0 or not control.is_visible(timeout=800):
            return []
        control.click(timeout=2000)
        page.wait_for_timeout(350)
        options = []
        for option_scope in get_apply_scopes(page):
            for option_selector in [
                '[role="option"]',
                '[data-automation-id*="promptOption"]',
                '[data-automation-id*="menuItem"]',
            ]:
                try:
                    values = option_scope.locator(option_selector).evaluate_all(
                        """
                        (nodes) => nodes
                          .filter((node) => {
                            const style = window.getComputedStyle(node);
                            const box = node.getBoundingClientRect();
                            return !!(box.width || box.height || node.getClientRects().length) &&
                              style.visibility !== 'hidden' && style.display !== 'none';
                          })
                          .map((node) => (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim())
                          .filter(Boolean)
                        """
                    )
                except Exception:
                    values = []
                for value in values:
                    if value and value not in options and not re.match(r"^(select|choose)( one| an option| an answer)?$", value, re.IGNORECASE):
                        options.append(value)
                    if len(options) >= max_options:
                        break
                if len(options) >= max_options:
                    break
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return options
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return []


def workday_schema_question_text(field):
    for candidate in [
        field.get("validation_message"),
        field.get("group_text"),
        field.get("raw_label"),
        field.get("label"),
    ]:
        text = clean_question_candidate(candidate)
        if text and not re.match(r"^(select|select one|required|select one required)$", normalize_question_text(text), re.IGNORECASE):
            return text
    return ""


def workday_questions_from_schema_fields(page, fields, stage, user_data):
    if stage != "application_questions" or DetectedQuestion is None:
        return []
    questions = []
    seen = set()
    for field in fields or []:
        control_type = str(field.get("input_type") or "").lower()
        if control_type not in {"select", "radio", "checkbox", "text", "textarea"}:
            continue
        if not field.get("required") or field.get("value_present") or field.get("disabled") or field.get("read_only"):
            continue
        text = workday_schema_question_text(field)
        if not text:
            continue
        options = list(field.get("options") or [])
        if control_type in {"select", "radio"} and not options:
            options = read_visible_workday_options_for_field(page, field)
        context = {
            "nearest_group_text": field.get("group_text") or "",
            "label_for_text": field.get("raw_label") or field.get("label") or "",
            "validation_message": field.get("validation_message") or "",
            "options": options,
            "current_stage": stage,
        }
        canonical_key = canonical_key_for_question(text, context)
        normalized_text = normalize_question_text(text)
        fingerprint = fingerprint_question(normalized_text, control_type, options)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        questions.append(DetectedQuestion(
            raw_text=text,
            normalized_text=normalized_text,
            fingerprint=fingerprint,
            required=True,
            control_type=control_type,
            options=options,
            validation_message=field.get("validation_message") or "",
            locator_hints={
                "field_id": field.get("field_id"),
                "id": field.get("id"),
                "name": field.get("name"),
                "selector": field.get("selector"),
            },
            canonical_key=canonical_key,
            question_context=context,
        ))
    return questions


def workday_detected_question_needs_schema_repair(question):
    text = " ".join([
        str(getattr(question, "raw_text", "") or ""),
        str(getattr(question, "normalized_text", "") or ""),
    ]).lower()
    return bool(
        "select one" in text
        and (
            "primaryquestionnaire" in text
            or re.search(r"\b[0-9a-f]{16,}\b", text)
            or len(normalize_question_text(text)) < 20
        )
    )


def matching_schema_field_for_question(question, fields):
    hints = getattr(question, "locator_hints", {}) or {}
    for field in fields or []:
        if hints.get("field_id") and hints.get("field_id") == field.get("field_id"):
            return field
        if hints.get("id") and hints.get("id") == field.get("id"):
            return field
        if hints.get("name") and hints.get("name") == field.get("name"):
            return field
    for field in fields or []:
        if field_matches_detected_question(field, question):
            return field
    return None


def repair_workday_detected_questions_from_schema(page, questions, fields, stage):
    if stage != "application_questions":
        return questions
    repaired = []
    seen = set()
    for question in questions or []:
        if workday_detected_question_needs_schema_repair(question):
            field = matching_schema_field_for_question(question, fields)
            text = workday_schema_question_text(field or {}) if field else ""
            if text:
                options = list(getattr(question, "options", None) or field.get("options") or [])
                if str(getattr(question, "control_type", "") or "").lower() in {"select", "radio"} and not options:
                    options = read_visible_workday_options_for_field(page, field)
                context = dict(getattr(question, "question_context", None) or {})
                context.update({
                    "nearest_group_text": field.get("group_text") or context.get("nearest_group_text") or "",
                    "label_for_text": field.get("raw_label") or field.get("label") or context.get("label_for_text") or "",
                    "validation_message": field.get("validation_message") or context.get("validation_message") or "",
                    "options": options,
                    "current_stage": stage,
                })
                question.raw_text = text
                question.normalized_text = normalize_question_text(text)
                question.options = options
                question.canonical_key = canonical_key_for_question(text, context)
                question.fingerprint = fingerprint_question(question.normalized_text, question.control_type, options)
                question.question_context = context
        if question.fingerprint in seen:
            continue
        seen.add(question.fingerprint)
        repaired.append(question)
    return repaired


def detect_application_question_blockers(page, fields, user_data, req, stage):
    if not detect_visible_required_questions:
        return None
    if not hasattr(page, "evaluate"):
        return None
    approved = approved_question_answers_from_user_data(user_data)
    try:
        detector_user_data = user_data
        if client and (
            config_bool(user_data_config_value(user_data, "enable_llm_question_canonicalizer"), default=False)
            or config_bool(user_data_config_value(user_data, "enable_llm_library_matcher"), default=False)
        ):
            detector_user_data = dict(user_data or {})
            detector_user_data["llm_question_canonicalizer"] = lambda context: llm_classify_question_context(context, user_data)
        questions = detect_visible_required_questions(page, approved_answers=approved, user_data=detector_user_data)
    except Exception as err:
        return {
            "status": NEEDS_TECHNICAL_REVIEW,
            "application_id": application_request_id(req, user_data),
            "batch_id": application_batch_id(req, user_data),
            "blocker_ids": [],
            "questions": [],
            "checkpoint": {"ats": infer_ats(page.url or ""), "stage": stage, "url": page.url},
            "detector_error": str(err)[:300],
        }
    if questions:
        questions = repair_workday_detected_questions_from_schema(page, questions, fields, stage)
    if not questions:
        questions = workday_questions_from_schema_fields(page, fields, stage, user_data)
    if not questions:
        return None
    blockers = []
    metadata_by_fingerprint = {}
    trusted_filled = []
    probe_filled = []
    probe_enabled = bool(getattr(req, "probe_fill_unapproved_questions", True))
    for question in questions:
        if getattr(question, "canonical_key", None) == "start_date":
            start_date_result = fill_workday_composite_date_question(page, question, user_data)
            if start_date_result.get("filled"):
                trusted_filled.append({
                    "fingerprint": question.fingerprint,
                    "field": question.raw_text,
                    "source": "profile_start_date",
                    "answer_source_key": start_date_result.get("source_key"),
                    "answer_match_method": "canonical_start_date",
                    "answer_match_confidence": 1.0,
                })
                continue
            question.status = TECHNICAL_REVIEW
            metadata_by_fingerprint[question.fingerprint] = {
                "start_date_fill_failed": True,
                "start_date_fill_reason": start_date_result.get("reason"),
                "answer_source_key": start_date_result.get("source_key"),
                "requires_user_review": True,
            }
            blockers.append(question)
            continue
        trusted_match = match_question_to_trusted_answer(question, user_data, options=question.options)
        if trusted_match.get("matched") and trusted_match.get("requires_review"):
            mapped_answer = map_trusted_answer_to_question_option(question, trusted_match.get("answer"))
            suggestion_metadata = {
                **trusted_answer_match_metadata(trusted_match),
                "suggested_answer": mapped_answer if mapped_answer not in (None, "") else trusted_match.get("answer"),
                "suggested_answer_requires_review": True,
                "requires_user_review": True,
            }
            if str(question.control_type or "").lower() in {"select", "radio"} and mapped_answer in (None, ""):
                question.status = TECHNICAL_REVIEW
                metadata_by_fingerprint[question.fingerprint] = {
                    **suggestion_metadata,
                    "trusted_answer_available": True,
                    "trusted_answer_option_mapping_failed": True,
                }
                blockers.append(question)
                continue
            question_text = " ".join([str(question.raw_text or ""), str(question.normalized_text or "")])
            if is_sensitive_question(question_text):
                metadata_by_fingerprint[question.fingerprint] = suggestion_metadata
                blockers.append(question)
                continue
            metadata_by_fingerprint[question.fingerprint] = suggestion_metadata
            trusted_match = trusted_answer_match_result(False, reason="Low-confidence non-sensitive suggestion deferred to probe.")
        if trusted_match.get("matched") and trusted_match.get("requires_review"):
            blockers.append(question)
            continue

        if trusted_match.get("matched"):
            trusted_answer = map_trusted_answer_to_question_option(question, trusted_match.get("answer"))
            if str(question.control_type or "").lower() in {"select", "radio"} and trusted_answer in (None, ""):
                question.status = TECHNICAL_REVIEW
                metadata_by_fingerprint[question.fingerprint] = {
                    **trusted_answer_match_metadata(trusted_match),
                    "trusted_answer_available": True,
                    "trusted_answer_option_mapping_failed": True,
                    "requires_user_review": True,
                }
                blockers.append(question)
                continue
            field = fill_detected_question_value(page, question, fields, trusted_answer, allow_confirmed_sensitive=True)
            if field:
                trusted_filled.append({
                    "fingerprint": question.fingerprint,
                    "field": field_key(field),
                    "source": "trusted_question_answer",
                    **trusted_answer_match_metadata(trusted_match),
                })
                continue
            question.status = TECHNICAL_REVIEW
            metadata_by_fingerprint[question.fingerprint] = {
                **trusted_answer_match_metadata(trusted_match),
                "trusted_answer_available": True,
                "trusted_answer_fill_failed": True,
                "requires_user_review": True,
            }
            blockers.append(question)
            continue

        stop_reason = probe_stop_reason_for_question(question)
        probe_value = probe_value_for_question(question) if probe_enabled and not stop_reason else None
        if probe_enabled and probe_value:
            field = fill_detected_question_value(page, question, fields, probe_value, allow_confirmed_sensitive=True)
            if field and not field_has_scoped_validation_error(page, field):
                metadata = {
                    **(metadata_by_fingerprint.get(question.fingerprint) or {}),
                    "probe_answer_applied": True,
                    "probe_answer": probe_value,
                    "requires_user_review": True,
                    "probe_control_type": question.control_type,
                }
                if str(question.control_type or "").lower() in {"select", "radio"}:
                    metadata.update({
                        "conditional_branch_probe": True,
                        "branch_probe_answer": probe_value,
                        "branch_probe_warning": PROBE_BRANCH_WARNING,
                    })
                metadata_by_fingerprint[question.fingerprint] = metadata
                probe_filled.append({
                    "fingerprint": question.fingerprint,
                    "field": field_key(field),
                    "probe_answer": probe_value,
                })
                blockers.append(question)
                continue
            if fields:
                question.status = TECHNICAL_REVIEW
            metadata_by_fingerprint[question.fingerprint] = {
                **(metadata_by_fingerprint.get(question.fingerprint) or {}),
                "probe_answer_applied": False,
                "probe_answer": probe_value,
                "probe_fill_failed": True,
                "requires_user_review": True,
            }
            blockers.append(question)
            continue

        if stop_reason:
            if stop_reason not in {"required_file_input", "sensitive_question_requires_trusted_answer"}:
                question.status = TECHNICAL_REVIEW
            metadata_by_fingerprint[question.fingerprint] = {
                **(metadata_by_fingerprint.get(question.fingerprint) or {}),
                "probe_answer_applied": False,
                "probe_stop_reason": stop_reason,
                "requires_user_review": True,
            }
        blockers.append(question)

    if not blockers:
        return {
            "status": None,
            "application_id": application_request_id(req, user_data),
            "batch_id": application_batch_id(req, user_data),
            "blocker_ids": [],
            "questions": [],
            "trusted_filled": trusted_filled,
            "probe_filled": probe_filled,
            "all_questions_resolved_by_trusted_answers": True,
        }
    outcome = build_question_blocker_outcome(page, blockers, user_data, req, stage, metadata_by_fingerprint=metadata_by_fingerprint)
    outcome["trusted_filled"] = trusted_filled
    outcome["probe_filled"] = probe_filled
    outcome["probe_mode"] = probe_enabled
    return outcome


def consolidated_question_blocker_bundle_from_pages(pages):
    bundle = {
        "status": BLOCKED_ON_QUESTIONS,
        "blocker_ids": [],
        "questions": [],
        "memory_results": [],
        "sources": [],
        "requires_final_review": False,
        "probe_filled_count": 0,
    }
    seen_ids = set()
    seen_questions = set()
    seen_probe_ids = set()
    has_technical_review = False
    for page_record in pages or []:
        question_blocker = (page_record.get("autofill") or {}).get("question_blocker") or page_record.get("question_blocker")
        if not question_blocker:
            continue
        bundle["sources"].append({
            "page_number": page_record.get("page_number"),
            "stage": page_record.get("stage") or page_record.get("stage_after") or page_record.get("stage_before"),
            "url": page_record.get("url"),
        })
        if question_blocker.get("status") == NEEDS_TECHNICAL_REVIEW:
            has_technical_review = True
        for blocker_id in question_blocker.get("blocker_ids") or []:
            if blocker_id not in seen_ids:
                seen_ids.add(blocker_id)
                bundle["blocker_ids"].append(blocker_id)
        for question in question_blocker.get("questions") or []:
            key = question.get("fingerprint") or question.get("normalized_text") or question.get("raw_text")
            if key and key in seen_questions:
                continue
            if key:
                seen_questions.add(key)
            bundle["questions"].append(question)
        for memory_result in question_blocker.get("memory_results") or []:
            bundle["memory_results"].append(memory_result)
            blocker = memory_result.get("blocker") or {}
            metadata = blocker.get("metadata") or {}
            probe_key = memory_result.get("id") or blocker.get("id") or memory_result.get("fingerprint")
            if metadata.get("probe_answer_applied") and probe_key and probe_key not in seen_probe_ids:
                seen_probe_ids.add(probe_key)
        bundle["requires_final_review"] = bundle["requires_final_review"] or bool(question_blocker.get("requires_final_review"))
    bundle["probe_filled_count"] = len(seen_probe_ids)
    if not bundle["blocker_ids"] and not bundle["questions"]:
        return None
    if has_technical_review:
        bundle["status"] = NEEDS_TECHNICAL_REVIEW
    return bundle


def unresolved_question_blocker_bundle(req, user_data, pages=None):
    local_bundle = consolidated_question_blocker_bundle_from_pages(pages)
    if local_bundle:
        return local_bundle
    app_id = application_request_id(req, user_data)
    memory_blockers = []
    if app_id:
        try:
            import requests
            for status in [UNANSWERED, TECHNICAL_REVIEW]:
                response = requests.get(
                    mongo_service_url("/question-blockers"),
                    params={"application_id": app_id, "status": status, "limit": 500, "offset": 0},
                    timeout=4,
                )
                if response.status_code < 400:
                    memory_blockers.extend((response.json() or {}).get("blockers") or [])
        except Exception as err:
            print(f"[Question Probe] Unresolved blocker lookup skipped: {err}")
    if not memory_blockers:
        return None
    memory_status = NEEDS_TECHNICAL_REVIEW if any(item.get("status") == TECHNICAL_REVIEW for item in memory_blockers) else BLOCKED_ON_QUESTIONS
    memory_bundle = {
        "status": memory_status,
        "application_id": app_id,
        "batch_id": application_batch_id(req, user_data),
        "blocker_ids": [item.get("id") for item in memory_blockers if item.get("id")],
        "questions": [
            {
                "raw_text": item.get("raw_text"),
                "normalized_text": item.get("normalized_text"),
                "fingerprint": item.get("fingerprint"),
                "control_type": item.get("control_type"),
                "options": item.get("options") or [],
                "status": item.get("status"),
                "metadata": item.get("metadata") or {},
            }
            for item in memory_blockers
        ],
        "blockers": memory_blockers,
        "requires_final_review": True,
        "probe_filled_count": sum(1 for item in memory_blockers if (item.get("metadata") or {}).get("probe_answer_applied")),
        "sources": [{"source": "memory_api"}],
    }
    return memory_bundle

def discover_application_steps(page, req, user_data):
    pages = []
    all_fields = []
    stop_reason = None
    signature_counts = {}
    visual_retried_signatures = set()

    for page_number in range(1, max(1, min(req.max_form_pages, 20)) + 1):
        write_live_smoke_progress(page, stage="discovery", action=f"page_{page_number}_start")
        dismiss_popups(page)
        step_interactive = wait_for_workday_step_interactive(page)
        resume_upload = handle_workday_autofill_with_resume_branch(page, req)
        if not resume_upload.get("attempted"):
            resume_upload = handle_resume_upload_prompt(page, req)
        if resume_upload.get("uploaded"):
            wait_for_apply_page_ready(page, timeout=20000, allow_reload=False)
            dismiss_popups(page)
            step_interactive = wait_for_workday_step_interactive(page)
        fields = extract_form_schema(page, user_data)
        blank_recovery = {"attempted": False, "reason": "workday_autofill_timeout"} if resume_upload.get("autofill_blank_timeout") else recover_workday_blank_apply_step(page)
        if blank_recovery.get("attempted"):
            dismiss_popups(page)
            fields = extract_form_schema(page, user_data)
        signature = apply_page_signature(page, fields)
        signature_count = signature_counts.get(signature, 0)
        if signature_count >= 2:
            is_workday_page = "myworkdayjobs.com" in (urlparse(page.url or "").hostname or "").lower()
            if (not is_workday_page) and signature not in visual_retried_signatures and page_has_validation_errors(page):
                visual_retried_signatures.add(signature)
                stage = infer_apply_stage(page, fields)
                visual_result = visual_field_fill_step(page, req, user_data, reason="repeated_form_page_validation")
                clicked, label = (False, "")
                if visual_result.get("acted"):
                    clicked, label = click_next_form_step(page)
                    if clicked:
                        page.wait_for_timeout(3000)
                        wait_for_apply_page_ready(page, timeout=25000, allow_reload=False)
                pages.append({
                    "page_number": page_number,
                    "url": page.url,
                    "stage": stage,
                    "field_count": len(fields),
                    "fields": fields,
                    "page_state": build_page_state(page, fields, user_data, stage=stage),
                    "preflight": build_preflight(page, fields, user_data, req, stage=stage),
                    "autofill": {
                        "filled": [{
                            "field": "visual_field_fallback",
                            "source": "vision",
                            "risk": "low",
                            "details": visual_result.get("applied")
                        }] if visual_result.get("acted") else [],
                        "missing_required": [],
                        "skipped": visual_result.get("skipped") or [],
                    },
                    "visual_field_fallback": visual_result,
                    "next_action": f"clicked:{label}" if clicked else None,
                    "screenshot_path": capture_apply_screenshot(page, f"apply_discovery_page_{page_number}"),
                })
                if clicked:
                    continue
            stop_reason = "repeated_form_page"
            break
        signature_counts[signature] = signature_count + 1

        stage = infer_apply_stage(page, fields)
        write_live_smoke_progress(page, stage=stage, action="build_preflight", field_count=len(fields))
        page_state = build_page_state(page, fields, user_data, stage=stage)
        preflight = build_preflight(page, fields, user_data, req, stage=stage)
        write_live_smoke_progress(page, stage=stage, action="fill_discovery_page_fields", field_count=len(fields))
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
            "autofill_resume_wait": resume_upload.get("autofill_resume_wait"),
            "blank_step_recovery": blank_recovery,
            "step_interactive": step_interactive,
            "screenshot_path": resume_upload.get("screenshot_path") or capture_apply_screenshot(page, f"apply_discovery_page_{page_number}"),
        }
        write_live_smoke_progress(
            page,
            stage=stage,
            action="page_recorded",
            screenshot_path=page_record.get("screenshot_path"),
            field_count=len(fields),
        )
        pages.append(page_record)
        all_fields.extend(fields)

        if page_has_final_submit(page):
            blocker_bundle = unresolved_question_blocker_bundle(req, user_data, pages)
            if blocker_bundle:
                page_record["question_blocker"] = blocker_bundle
                stop_reason = "application_question_blocker"
                break
            if workday_review_has_missing_education(page, user_data) and click_workday_progress_step(page, "My Experience"):
                page_record["review_repair"] = "education_no_response"
                page_record["next_action"] = "clicked:My Experience (repair education)"
                continue
            if workday_review_has_bad_highest_education(page) and click_workday_progress_step(page, "Application Questions 2 of 2"):
                page_record["review_repair"] = "highest_education_partial_bachelor"
                page_record["next_action"] = "clicked:Application Questions 2 of 2 (repair highest education)"
                continue
            stop_reason = "final_submit_guard"
            break

        has_high_risk_missing = any(item.get("risk") == "high" for item in fill_result["missing_required"])
        if has_high_risk_missing:
            stop_reason = "application_question_blocker" if fill_result.get("question_blocker") else "high_risk_required_fields"
            break

        protected_country_mismatch = next(
            (item for item in fill_result["missing_required"] if item.get("reason") == "protected_country_mismatch"),
            None,
        )
        if protected_country_mismatch:
            stop_reason = "protected_country_mismatch"
            page_record["protected_country_mismatch"] = protected_country_mismatch
            break

        if fill_result["missing_required"]:
            stop_reason = "application_question_blocker" if fill_result.get("question_blocker") else "missing_required_fields"
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
        write_live_smoke_progress(page, stage=stage, action=f"clicked:{label}", screenshot_path=page_record.get("screenshot_path"))
        page.wait_for_timeout(3000)
        wait_for_apply_page_ready(page, timeout=25000, allow_reload=False)

    return {
        "pages": pages,
        "fields": all_fields,
        "stop_reason": stop_reason or "max_form_pages_reached",
        "question_blocker": unresolved_question_blocker_bundle(req, user_data, pages) if stop_reason == "application_question_blocker" else None,
        "protected_country_mismatch": next(
            (
                page.get("protected_country_mismatch")
                for page in pages
                if page.get("protected_country_mismatch")
            ),
            None,
        ),
    }

def submit_application_steps(page, req, user_data):
    pages = []
    all_fields = []
    seen = set()
    max_pages = max(1, min(getattr(req, "max_form_pages", 8), 20))

    for page_number in range(1, max_pages + 1):
        readiness = wait_for_apply_page_ready(page, timeout=25000, allow_reload=False)
        dismiss_popups(page)
        step_interactive = wait_for_workday_step_interactive(page)
        resume_upload = handle_workday_autofill_with_resume_branch(page, req)
        if not resume_upload.get("attempted"):
            resume_upload = handle_resume_upload_prompt(page, req)
        if resume_upload.get("uploaded"):
            wait_for_apply_page_ready(page, timeout=25000, allow_reload=False)
            dismiss_popups(page)
            step_interactive = wait_for_workday_step_interactive(page)

        fields_before = extract_form_schema(page, user_data)
        blank_recovery = {"attempted": False, "reason": "workday_autofill_timeout"} if resume_upload.get("autofill_blank_timeout") else recover_workday_blank_apply_step(page)
        if blank_recovery.get("attempted"):
            dismiss_popups(page)
            fields_before = extract_form_schema(page, user_data)
        signature = apply_page_signature(page, fields_before)
        if signature in seen:
            screenshot_path = capture_apply_screenshot(page, "apply_submit_repeated_page")
            return {
                "success": False,
                "status": "Blocked",
                "blocked_reason": "repeated_form_page",
                "stage": infer_apply_stage(page, fields_before),
                "fields": all_fields or fields_before,
                "pages": pages,
                "screenshot_path": screenshot_path,
                "method": "structured_submit_state_machine",
            }
        seen.add(signature)

        stage_before = infer_apply_stage(page, fields_before)
        preflight_before = build_preflight(page, fields_before, user_data, req, stage=stage_before)
        fill_result = fill_discovery_page_fields(page, fields_before, user_data, req)
        page.wait_for_timeout(900)

        fields_after = extract_form_schema(page, user_data)
        stage_after = infer_apply_stage(page, fields_after)
        page_state_after = build_page_state(page, fields_after, user_data, stage=stage_after)
        preflight_after = build_preflight(page, fields_after, user_data, req, stage=stage_after)
        blocking = non_final_blocking_issues(preflight_after)

        page_record = {
            "page_number": page_number,
            "url": page.url,
            "stage_before": stage_before,
            "stage_after": stage_after,
            "readiness": readiness,
            "field_count_before": len(fields_before),
            "field_count_after": len(fields_after),
            "preflight_before": preflight_before,
            "preflight_after": preflight_after,
            "autofill": fill_result,
            "resume_upload": resume_upload,
            "autofill_resume_wait": resume_upload.get("autofill_resume_wait"),
            "blank_step_recovery": blank_recovery,
            "step_interactive": step_interactive,
        }
        pages.append(page_record)
        all_fields.extend(fields_after or fields_before)

        if stage_after == "blocked_captcha":
            screenshot_path = capture_apply_screenshot(page, "apply_submit_captcha")
            return {
                "success": False,
                "status": "Blocked",
                "blocked_reason": "captcha_or_bot_challenge",
                "stage": stage_after,
                "fields": fields_after,
                "pages": pages,
                "page_state": page_state_after,
                "preflight": preflight_after,
                "screenshot_path": screenshot_path,
                "method": "structured_submit_state_machine",
            }

        missing_required = fill_result.get("missing_required", [])
        if missing_required or blocking:
            question_blocker = fill_result.get("question_blocker")
            screenshot_path = capture_apply_screenshot(page, "apply_submit_needs_user")
            return {
                "success": False,
                "status": (question_blocker or {}).get("status") or "Blocked",
                "blocked_reason": "application_question_blocker" if question_blocker else "missing_or_unconfirmed_required_fields",
                "blocking_issues": blocking,
                "missing_required": missing_required,
                "question_blocker": question_blocker,
                "stage": stage_after,
                "fields": fields_after,
                "pages": pages,
                "page_state": page_state_after,
                "preflight": preflight_after,
                "screenshot_path": screenshot_path,
                "method": "structured_submit_state_machine",
            }

        if page_has_final_submit(page):
            blocker_bundle = unresolved_question_blocker_bundle(req, user_data, pages)
            if blocker_bundle:
                screenshot_path = capture_apply_screenshot(page, "apply_submit_question_blocker_review")
                return {
                    "success": False,
                    "status": blocker_bundle.get("status") or BLOCKED_ON_QUESTIONS,
                    "blocked_reason": "application_question_blocker",
                    "stage": stage_after,
                    "fields": fields_after,
                    "pages": pages,
                    "page_state": page_state_after,
                    "preflight": preflight_after,
                    "question_blocker": blocker_bundle,
                    "screenshot_path": screenshot_path,
                    "method": "structured_submit_state_machine",
                }
            if not getattr(req, "confirm_submit", False):
                screenshot_path = capture_apply_screenshot(page, "apply_submit_confirmation_required")
                return {
                    "success": False,
                    "status": READY_TO_SUBMIT,
                    "blocked_reason": "final_submit_confirmation_required",
                    "stage": stage_after,
                    "fields": fields_after,
                    "pages": pages,
                    "page_state": page_state_after,
                    "preflight": preflight_after,
                    "screenshot_path": screenshot_path,
                    "method": "structured_submit_state_machine",
                }
            clicked, label = click_final_submit_control(page)
            page_record["final_submit_action"] = f"clicked:{label}" if clicked else "not_clicked"
            if not clicked:
                screenshot_path = capture_apply_screenshot(page, "apply_submit_no_final_control")
                return {
                    "success": False,
                    "status": "Blocked",
                    "blocked_reason": "final_submit_control_not_clickable",
                    "stage": stage_after,
                    "fields": fields_after,
                    "pages": pages,
                    "page_state": page_state_after,
                    "preflight": preflight_after,
                    "screenshot_path": screenshot_path,
                    "method": "structured_submit_state_machine",
                }
            wait_for_apply_page_ready(page, timeout=30000, allow_reload=False)
            page.wait_for_timeout(3000)
            validation_errors = extract_validation_errors(page)
            success = submission_success_detected(page)
            screenshot_path = capture_apply_screenshot(page, "apply_submit_final")
            if success:
                return {
                    "success": True,
                    "status": "Submitted",
                    "blocked_reason": None,
                    "stage": "submitted",
                    "fields": fields_after,
                    "pages": pages,
                    "page_state": build_page_state(page, extract_form_schema(page, user_data), user_data, stage="submitted"),
                    "preflight": preflight_after,
                    "screenshot_path": screenshot_path,
                    "method": "structured_submit_state_machine",
                    "final_submit_label": label,
                }
            return {
                "success": False,
                "status": "Unconfirmed",
                "blocked_reason": "submission_success_not_detected",
                "stage": infer_apply_stage(page, extract_form_schema(page, user_data)),
                "fields": extract_form_schema(page, user_data),
                "pages": pages,
                "page_state": build_page_state(page, extract_form_schema(page, user_data), user_data),
                "preflight": preflight_after,
                "validation_errors": validation_errors,
                "screenshot_path": screenshot_path,
                "method": "structured_submit_state_machine",
                "final_submit_label": label,
            }

        clicked, label = click_next_form_step(page)
        page_record["next_action"] = f"clicked:{label}" if clicked else "not_clicked"
        if not clicked:
            if submission_success_detected(page):
                screenshot_path = capture_apply_screenshot(page, "apply_submit_success")
                return {
                    "success": True,
                    "status": "Submitted",
                    "blocked_reason": None,
                    "stage": "submitted",
                    "fields": fields_after,
                    "pages": pages,
                    "page_state": page_state_after,
                    "preflight": preflight_after,
                    "screenshot_path": screenshot_path,
                    "method": "structured_submit_state_machine",
                }
            screenshot_path = capture_apply_screenshot(page, "apply_submit_no_next")
            return {
                "success": False,
                "status": "Blocked",
                "blocked_reason": "no_next_or_final_submit_control",
                "stage": stage_after,
                "fields": fields_after,
                "pages": pages,
                "page_state": page_state_after,
                "preflight": preflight_after,
                "screenshot_path": screenshot_path,
                "method": "structured_submit_state_machine",
            }
        wait_for_apply_page_ready(page, timeout=30000, allow_reload=False)

    screenshot_path = capture_apply_screenshot(page, "apply_submit_max_pages")
    fields = extract_form_schema(page, user_data)
    stage = infer_apply_stage(page, fields)
    return {
        "success": False,
        "status": "Blocked",
        "blocked_reason": "max_form_pages_reached",
        "stage": stage,
        "fields": fields,
        "pages": pages,
        "page_state": build_page_state(page, fields, user_data, stage=stage),
        "preflight": build_preflight(page, fields, user_data, req, stage=stage),
        "screenshot_path": screenshot_path,
        "method": "structured_submit_state_machine",
    }

def run_apply_access_state_machine(page, req, user_data):
    write_live_smoke_progress(page, stage="access_start", action="state_machine_start")
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
        write_live_smoke_progress(page, stage=stage, action=f"access_step_{step + 1}", field_count=len(fields))
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

        if is_blank_workday_autofill_branch(page, fields):
            write_live_smoke_progress(page, stage=stage, action="handle_workday_autofill_with_resume_branch")
            resume_upload = handle_workday_autofill_with_resume_branch(page, req)
            history[-1]["resume_upload"] = resume_upload
            history[-1]["autofill_resume_wait"] = resume_upload.get("autofill_resume_wait")
            if resume_upload.get("ready"):
                history[-1]["action"] = "waited_for_autofill_resume_result"
                continue
            apply_shell_url = re.sub(r"/autofillWithResume/?$", "", page.url or "", flags=re.IGNORECASE)
            history[-1]["action"] = "recover_blank_autofill_to_apply_shell"
            history[-1]["autofill_blank_timeout"] = bool(resume_upload.get("autofill_blank_timeout"))
            history[-1]["screenshot_path"] = resume_upload.get("screenshot_path")
            history[-1]["apply_shell_url"] = apply_shell_url
            page.goto(apply_shell_url or req.url, wait_until="domcontentloaded", timeout=45000)
            wait_for_apply_page_ready(page, timeout=45000, allow_reload=True)
            continue

        if stage == "unknown" and not fields:
            parsed_context = urlparse(page.url or "")
            context_path = (parsed_context.path or "").lower()
            is_workday_apply_flow = "myworkdayjobs.com" in (parsed_context.hostname or "").lower() and (context_path.endswith("/apply") or "/apply/" in context_path)
            prior_workday_waits = sum(
                1 for item in history[:-1]
                if item.get("action") == "wait_for_workday_apply_fields"
            )
            if is_workday_apply_flow and prior_workday_waits < 2:
                history[-1]["action"] = "wait_for_workday_apply_fields"
                page.wait_for_timeout(5000)
                continue
            prior_blank_apply_retries = sum(
                1 for item in history[:-1]
                if item.get("action") == "return_to_original_job_after_blank_workday_apply"
            )
            attempted_login_or_account = any(
                item.get("login_attempt")
                or item.get("account_creation_attempt")
                or item.get("stage") in {"sign_in", "create_account"}
                for item in history[:-1]
            )
            if is_workday_apply_flow and attempted_login_or_account and prior_blank_apply_retries < 2:
                history[-1]["action"] = "return_to_original_job_after_blank_workday_apply"
                page.goto(req.url, wait_until="domcontentloaded", timeout=45000)
                wait_for_apply_page_ready(page, timeout=45000, allow_reload=True)
                continue

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
            write_live_smoke_progress(page, stage=stage, action="returned_to_original_job_after_auth")
            page.goto(req.url, wait_until="domcontentloaded", timeout=45000)
            wait_for_apply_page_ready(page, timeout=45000, allow_reload=True)
            continue

        if is_application_flow_stage(stage):
            write_live_smoke_progress(page, stage=stage, action="application_flow_stage")
            account_record = remember_apply_account(account_key, account_meta, event="form_access")
            account_known = bool(account_record.get("account_created") or account_record.get("account_exists"))
            discovery = None
            result_fields = fields
            if req.discover_all_steps or not getattr(req, "stop_at_form", True):
                write_live_smoke_progress(page, stage=stage, action="discover_application_steps_start")
                discovery = discover_application_steps(page, req, user_data)
                result_fields = discovery.get("fields", fields)
                write_live_smoke_progress(page, stage=stage, action="discover_application_steps_done")
                if discovery.get("stop_reason") == "protected_country_mismatch":
                    issue = discovery.get("protected_country_mismatch") or {}
                    return {
                        "success": False,
                        "status": NEEDS_TECHNICAL_REVIEW,
                        "stage": "MY_INFORMATION",
                        "blocked_reason": "protected_country_mismatch",
                        "expected_country": issue.get("expected_country") or "United States of America",
                        "actual_country": issue.get("actual_country") or "",
                        "fields": result_fields,
                        "history": history,
                        "account": sanitized_account_status(account_key, account_record),
                        "discovery": discovery,
                        "page_state": last_page_state,
                        "preflight": last_preflight,
                    }
                if discovery.get("stop_reason") == "application_question_blocker":
                    question_blocker = discovery.get("question_blocker") or unresolved_question_blocker_bundle(req, user_data, discovery.get("pages"))
                    return {
                        "success": False,
                        "status": (question_blocker or {}).get("status") or BLOCKED_ON_QUESTIONS,
                        "stage": ((question_blocker or {}).get("checkpoint") or {}).get("stage") or stage,
                        "blocked_reason": "application_question_blocker",
                        "fields": result_fields,
                        "history": history,
                        "account": sanitized_account_status(account_key, account_record),
                        "discovery": discovery,
                        "page_state": last_page_state,
                        "preflight": last_preflight,
                        "question_blocker": question_blocker,
                    }
            return {
                "success": True,
                "status": "form_detected",
                "stage": stage if stage != "application_form" else "application_form",
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
                write_live_smoke_progress(page, stage=stage, action=f"clicked:{label}")
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
            sign_in_attempt_count = sum(
                1 for item in history[:-1]
                if item.get("stage") == "sign_in" and "login_attempt" in item
            )
            attempted_login = sign_in_attempt_count > 0
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
            if (account_known or attempted_create) and sign_in_attempt_count < 3:
                login_password, password_adjusted = adapt_password_to_visible_policy(
                    page, password, enabled=req.adjust_password_to_policy
                )
                fill_auth_identity(page, user_data, login_password)
                clicked_login, label = click_matching_control(page, [
                    r"^sign in$", r"^log in$", r"^login$"
                ], skip_final_submit=True, avoid_patterns=[
                    r"linkedin", r"facebook", r"google", r"single sign", r"sso"
                ])
                if not clicked_login:
                    clicked_login, label = click_workday_sign_in_submit(page)
                if clicked_login:
                    history[-1]["login_attempt"] = sign_in_attempt_count + 1
                    history[-1]["password_policy_adjusted"] = password_adjusted
                    history[-1]["password_source"] = password_source
                    history[-1]["action"] = f"clicked:{label}"
                    write_live_smoke_progress(page, stage=stage, action=f"clicked:{label}")
                    pending_account_event = "login"
                    pending_account_password = login_password
                    continue
            if sign_in_attempt_count >= 3:
                blocked_reason = "sign_in_failed_after_retries"
                break

            if not account_known:
                clicked_create, label = click_matching_control(page, [
                    r"create account", r"create profile", r"new user", r"register", r"sign up",
                    r"don'?t have an account", r"no account", r"create one"
                ], skip_final_submit=True)
                if clicked_create:
                    history[-1]["action"] = f"clicked:{label}"
                    write_live_smoke_progress(page, stage=stage, action=f"clicked:{label}")
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
                    write_live_smoke_progress(page, stage=stage, action=f"clicked:{label}")
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
                write_live_smoke_progress(page, stage=stage, action=f"clicked:{label}")
                pending_account_event = "created"
                pending_account_password = account_password
                continue
            blocked_reason = "create_account_no_action"
            break

        if stage in {"job_detail", "unknown"}:
            prefer_resume = workday_resume_autofill_allowed(req, history)
            clicked_workday_choice, workday_choice = click_workday_application_choice(page, prefer_resume=prefer_resume)
            if clicked_workday_choice:
                history[-1]["action"] = f"clicked:{workday_choice}"
                write_live_smoke_progress(page, stage=stage, action=f"clicked:{workday_choice}")
                continue
            if fields:
                account_record = remember_apply_account(account_key, account_meta, event="form_access")
                discovery = None
                result_fields = fields
                if req.discover_all_steps:
                    discovery = discover_application_steps(page, req, user_data)
                    result_fields = discovery.get("fields", fields)
                    if discovery.get("stop_reason") == "protected_country_mismatch":
                        issue = discovery.get("protected_country_mismatch") or {}
                        return {
                            "success": False,
                            "status": NEEDS_TECHNICAL_REVIEW,
                            "stage": "MY_INFORMATION",
                            "blocked_reason": "protected_country_mismatch",
                            "expected_country": issue.get("expected_country") or "United States of America",
                            "actual_country": issue.get("actual_country") or "",
                            "fields": result_fields,
                            "history": history,
                            "account": sanitized_account_status(account_key, account_record),
                            "discovery": discovery,
                            "page_state": last_page_state,
                            "preflight": last_preflight,
                        }
                return {
                    "success": True,
                    "status": "form_detected",
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
                write_live_smoke_progress(page, stage=stage, action=f"clicked:{label}")
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

REAL_SUBMIT_BLOCKED_URL_PARTS = (
    "localhost",
    "127.0.0.1",
    "/mock-form",
    "example.com/demo",
    "linkedin.com/jobs/search",
    "linkedin.com/jobs/view",
)

def is_real_external_submit_url(url):
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc:
        return False
    lowered = f"{parsed.netloc}{parsed.path}".lower()
    return not any(part in lowered for part in REAL_SUBMIT_BLOCKED_URL_PARTS)

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
    application_id: Optional[str] = None
    batch_id: Optional[str] = None
    application_password: Optional[str] = None
    security_answers: Optional[List[str]] = None
    cookies: list = []
    confirm_submit: bool = False
    max_steps: int = 14
    wait_for_email_seconds: int = 75
    allow_account_creation: bool = True
    allow_email_verification: bool = True
    allow_terms_acceptance: bool = True
    allow_security_question_autofill: bool = True
    adjust_password_to_policy: bool = True
    stop_at_form: bool = False
    discover_all_steps: bool = False
    max_form_pages: int = 8
    allow_low_risk_autofill: bool = True
    allow_placeholder_autofill: bool = False
    allow_visual_fallback: bool = False
    allow_visual_field_fallback: bool = True
    allow_resume_upload: bool = True
    probe_fill_unapproved_questions: bool = True

class AccessApplyRequest(BaseModel):
    url: str
    user_data: dict = {}
    application_id: Optional[str] = None
    batch_id: Optional[str] = None
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
    allow_visual_fallback: bool = False
    allow_visual_field_fallback: bool = True
    allow_resume_upload: bool = True
    probe_fill_unapproved_questions: bool = True
    confirm_submit: bool = False

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
    if (os.getenv("ALLOW_MOCK_FORM") or "false").strip().lower() not in {"1", "true", "yes", "on"}:
        raise HTTPException(status_code=404, detail="Mock form is disabled in real apply mode.")
    return MOCK_FORM_HTML

@app.post("/access-apply-form")
def access_apply_form(req: AccessApplyRequest):
    default_user_data = load_default_user_data()
    user_data = ensure_resume_text({**default_user_data, **(req.user_data or {})}, req.resume_path)
    reset_workday_adapter_state(user_data)
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
                viewport={"width": 1280, "height": 1100},
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
            write_live_smoke_progress(page, stage="navigate", action="goto_start", dom_excerpt="")
            page.goto(req.url, wait_until="domcontentloaded", timeout=45000)
            write_live_smoke_progress(page, stage="navigate", action="goto_done")
            initial_readiness = wait_for_apply_page_ready(page, timeout=90000, allow_reload=True)
            write_live_smoke_progress(page, stage="navigate", action="initial_readiness_done")
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
                "status": result.get("status") or ("form_detected" if result.get("success") else "blocked_or_incomplete"),
                "ats": infer_ats(page.url or req.url),
                "original_url": req.url,
                "current_url": page.url,
                "stage": result.get("stage"),
                "blocked_reason": result.get("blocked_reason"),
                "expected_country": result.get("expected_country"),
                "actual_country": result.get("actual_country"),
                "fields": result.get("fields", []),
                "history": result.get("history", []),
                "account": result.get("account", {}),
                "discovery": result.get("discovery"),
                "page_state": result.get("page_state") or current_page_state,
                "preflight": result.get("preflight") or current_preflight,
                "current_page_state": current_page_state,
                "current_preflight": current_preflight,
                "workday_select_debug": debug_workday_country_state_controls(page) if "myworkdayjobs.com" in (urlparse(page.url or "").hostname or "").lower() else [],
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
    if not is_real_external_submit_url(req.url):
        raise HTTPException(
            status_code=400,
            detail="Refusing to submit: /apply requires a real external ATS URL, not a mock/demo/local/LinkedIn job page."
        )
    if not req.confirm_submit:
        raise HTTPException(
            status_code=409,
            detail="Refusing to submit without explicit confirmation. Run /access-apply-form first, review required fields, then call /apply with confirm_submit=true."
        )
    default_user_data = load_default_user_data()
    user_data = ensure_resume_text({**default_user_data, **(req.user_data or {})}, req.resume_path)
    reset_workday_adapter_state(user_data)
    if not user_data.get("email"):
        raise HTTPException(status_code=400, detail="user_data.email is required for structured apply submission")

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
                viewport={"width": 1280, "height": 1100},
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
            page.goto(req.url, wait_until="domcontentloaded", timeout=45000)
            initial_readiness = wait_for_apply_page_ready(page, timeout=90000, allow_reload=True)
            req.allow_visual_fallback = False
            req.discover_all_steps = False
            req.stop_at_form = False
            req.allow_placeholder_autofill = False

            access_result = run_apply_access_state_machine(page, req, user_data)
            if not access_result.get("success"):
                fields = extract_form_schema(page, user_data)
                stage = infer_apply_stage(page, fields)
                screenshot_path = capture_apply_screenshot(page, "apply_access_blocked")
                return {
                    "success": False,
                    "status": "Blocked",
                    "ats": infer_ats(page.url or req.url),
                    "original_url": req.url,
                    "current_url": page.url,
                    "stage": access_result.get("stage") or stage,
                    "blocked_reason": access_result.get("blocked_reason") or "access_state_machine_blocked",
                    "fields": access_result.get("fields") or fields,
                    "history": access_result.get("history", []),
                    "account": access_result.get("account", {}),
                    "page_state": access_result.get("page_state") or build_page_state(page, fields, user_data, stage=stage),
                    "preflight": access_result.get("preflight") or build_preflight(page, fields, user_data, req, stage=stage),
                    "screenshot_path": screenshot_path,
                    "method": "structured_submit_state_machine",
                    "initial_readiness": initial_readiness,
                }

            submit_result = submit_application_steps(page, req, user_data)
            response = {
                "success": submit_result.get("success", False),
                "status": submit_result.get("status", "Blocked"),
                "ats": infer_ats(page.url or req.url),
                "original_url": req.url,
                "current_url": page.url,
                "stage": submit_result.get("stage"),
                "blocked_reason": submit_result.get("blocked_reason"),
                "fields": submit_result.get("fields", []),
                "history": access_result.get("history", []),
                "account": access_result.get("account", {}),
                "page_state": submit_result.get("page_state"),
                "preflight": submit_result.get("preflight"),
                "submit_pages": submit_result.get("pages", []),
                "screenshot_path": submit_result.get("screenshot_path"),
                "method": submit_result.get("method", "structured_submit_state_machine"),
                "initial_readiness": initial_readiness,
                "access_stage": access_result.get("stage"),
                "final_submit_label": submit_result.get("final_submit_label"),
            }
            for optional_key in ["blocking_issues", "missing_required", "validation_errors", "question_blocker"]:
                if optional_key in submit_result:
                    response[optional_key] = submit_result.get(optional_key)
            return response

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
                viewport={"width": 1280, "height": 1100},
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
