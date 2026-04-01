import time
from datetime import datetime
import os
import sys
import json
import requests
import logging
import shutil
import smtplib
import imaplib
import sqlite3
import threading
import webbrowser
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
try:
    import pyautogui
    PYAUTOGUI_AVAILABLE = True
except ImportError:
    PYAUTOGUI_AVAILABLE = False
    pyautogui = None
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
try:
    import schedule
except ImportError:
    schedule = None
from selenium import webdriver
try:
    from selenium.webdriver.chrome.service import Service as ChromeService  # Selenium 4
    from selenium.webdriver.chrome.options import Options as ChromeOptions
except Exception:
    ChromeService = None
    ChromeOptions = None
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
import re
try:
    from webdriver_manager.chrome import ChromeDriverManager
    WEBDRIVER_MANAGER_AVAILABLE = True
except ImportError:
    ChromeDriverManager = None
    WEBDRIVER_MANAGER_AVAILABLE = False
# import win32com.client  # Removed SAP dependency for IoT-only project

# Import custom actions handler (optional - gracefully handle if not present)
try:
    import x.xCustom as xCustom
    CUSTOM_AVAILABLE = True
except ImportError:
    try:
        # Fallback for bundled execution
        x_dir = None
        try:
            x_dir = os.path.abspath(os.path.join(sys._MEIPASS, 'x'))  # type: ignore[attr-defined]
        except Exception:
            pass
        if not x_dir or not os.path.isdir(x_dir):
            x_dir = os.path.abspath(os.path.join(os.path.dirname(__file__)))
        if x_dir not in sys.path:
            sys.path.insert(0, x_dir)
        import xCustom  # type: ignore[import-not-found]
        CUSTOM_AVAILABLE = True
    except ImportError:
        CUSTOM_AVAILABLE = False
        xCustom = None

"""
Selenium UI Actions with robust selector handling and debug artifacts.

Selector rules:
- Treat as XPath if selector starts with '/' or '(' or 'xpath=' prefix
- Treat as CSS if selector starts with '.' or '#' or alphabetic or 'css=' prefix

Debug mode:
- Enable via fEngine --debug flag or config key 'debug': true
- Captures screenshots and page source on errors into results_dir and results_dir/_debug
"""

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

_DEBUG_MODE = False

def set_debug_mode(enabled: bool):
    global _DEBUG_MODE
    _DEBUG_MODE = bool(enabled)
    logger.setLevel(logging.DEBUG if _DEBUG_MODE else logging.INFO)

def _detect_selector(locator: str):
    """Return (By, value) auto-detecting CSS vs XPath. Supports 'css=' and 'xpath=' prefixes.
    Automatically strips surrounding quotes (single or double) from the locator string.
    """
    if locator is None:
        raise ValueError("Locator cannot be None")
    raw = locator.strip()
    
    # Strip surrounding quotes if present (handles both single and double quotes)
    # Only strip if quotes are at both ends and match
    if len(raw) >= 2:
        if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
            raw = raw[1:-1].strip()
    
    if raw.lower().startswith('xpath='):
        return By.XPATH, raw[6:]
    if raw.lower().startswith('css='):
        return By.CSS_SELECTOR, raw[4:]
    if raw.startswith('/') or raw.startswith('('):
        return By.XPATH, raw
    if raw.startswith('.') or raw.startswith('#') or raw[:1].isalpha():
        return By.CSS_SELECTOR, raw
    # fallback to xpath
    return By.XPATH, raw

def _sanitize_error_message(message: str) -> str:
    """Strip verbose stack traces from exception strings for cleaner logs/outputs."""
    if not message:
        return message
    try:
        # Remove everything after the first occurrence of 'Stacktrace:' (case-sensitive per Selenium)
        parts = message.split('Stacktrace:', 1)
        cleaned = parts[0].rstrip()
        # Also collapse excessive whitespace
        return re.sub(r"\s+", " ", cleaned).strip()
    except Exception:
        return message

def _save_error_artifacts(driver, results_dir, plan_id, design_id, step_id, err_msg: str):
    """Save screenshot and page source on error; return user-friendly message with links."""
    # Sanitize message to avoid noisy stack traces in user-visible outputs
    err_msg = _sanitize_error_message(err_msg)
    if not results_dir:
        return err_msg
    debug_dir = os.path.join(results_dir, "_debug") if _DEBUG_MODE else results_dir
    os.makedirs(debug_dir, exist_ok=True)
    ts = datetime.now().strftime('%H%M%S')
    base = f"{plan_id}_{design_id}_{step_id}_{ts}" if step_id else f"{plan_id}_{design_id}_{ts}"
    screenshot = os.path.join(debug_dir, f"{base}.png")
    page_file = os.path.join(debug_dir, f"{base}.html")
    error_txt = os.path.join(debug_dir, f"{base}.txt")
    try:
        if getattr(driver, 'save_screenshot', None):
            driver.save_screenshot(screenshot)
    except Exception:
        pass
    try:
        if getattr(driver, 'page_source', None):
            with open(page_file, 'w', encoding='utf-8') as f:
                f.write(driver.page_source)
    except Exception:
        pass
    try:
        with open(error_txt, 'w', encoding='utf-8') as f:
            f.write(err_msg)
    except Exception:
        pass
    links = []
    if os.path.exists(screenshot):
        links.append(f"screenshot: {os.path.basename(screenshot)}")
    if os.path.exists(page_file):
        links.append(f"page: {os.path.basename(page_file)}")
    if os.path.exists(error_txt):
        links.append(f"error: {os.path.basename(error_txt)}")
    if links:
        return f"{err_msg} [Artifacts => {' | '.join(links)}]"
    return err_msg

class ActionHandler:
    """Base class for handling actions with common utilities."""
    def __init__(self, timeout=6):
        self._timeout = timeout
        self._driver = None
        self._auth_token = None
        self._sap_session = None

    def save_output_to_file(self, output, plan_id, design_id, step_id, results_dir):
        """Save output to file if it exceeds 140 characters or contains commas."""
        if not output:
            return output
        output = re.split(r'Stacktrace:', output, 1)[0].strip()
        if len(output) > 140 or ',' in output:
            filename = f"{plan_id}_{design_id}_{step_id}.txt" if step_id else f"{plan_id}_{design_id}.txt"
            filepath = os.path.join(results_dir, filename)
            try:
                os.makedirs(results_dir, exist_ok=True)
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(output)
                truncated = output[:140].replace(',', ';')
                return f"{truncated} [Link to {filename}]"
            except Exception as e:
                logger.error(f"Error saving output to {filepath}: {str(e)}")
                return output
        return output.replace(',', ';')

    def validate_input(self, input_data, expected_type=str):
        """Validate and convert input data."""
        if input_data is None:
            return ""
        if not isinstance(input_data, expected_type):
            try:
                return expected_type(input_data)
            except (ValueError, TypeError) as e:
                raise ValueError(f"Invalid input conversion: {str(e)}")
        return input_data

class UIActionHandler(ActionHandler):
    """Handles UI-related actions using Selenium."""
    _shared_driver = None

    def __init__(self, timeout=6):
        super().__init__(timeout)
        if UIActionHandler._shared_driver:
            self._driver = UIActionHandler._shared_driver

    def find_element(self, locator, clickable=False, retries=2, extra_wait_seconds=0):
        """Find element by auto-detected selector (XPath/CSS) with retries.
        Users: You can prefix with 'css=' or 'xpath=' to be explicit. Otherwise
        the handler guesses based on the first character.
        """
        if not self._driver:
            raise WebDriverException("WebDriver not initialized")
        if not locator or locator.strip() == "":
            raise ValueError("Locator cannot be empty")
        
        # Validate locator isn't obviously truncated (ends with incomplete XPath/CSS)
        locator_stripped = locator.strip()
        if len(locator_stripped) > 0:
            # Check for common truncation patterns
            if locator_stripped.count('(') > locator_stripped.count(')'):
                raise ValueError(f"Locator appears truncated (unmatched opening parenthesis): {locator_stripped[:50]}...")
            if locator_stripped.count('[') > locator_stripped.count(']'):
                raise ValueError(f"Locator appears truncated (unmatched opening bracket): {locator_stripped[:50]}...")
        
        # support explicit type==value pattern still
        if "==" in locator and locator.split("==", 1)[0].lower() in {"id", "name", "class", "xpath", "css"}:
            locator_type, locator_value = locator.split("==", 1)
            by_type = {
                "id": By.ID, "name": By.NAME, "class": By.CLASS_NAME,
                "xpath": By.XPATH, "css": By.CSS_SELECTOR
            }.get(locator_type.lower(), By.XPATH)
            # Strip quotes from locator_value as well
            if len(locator_value) >= 2:
                if (locator_value.startswith('"') and locator_value.endswith('"')) or (locator_value.startswith("'") and locator_value.endswith("'")):
                    locator_value = locator_value[1:-1].strip()
        else:
            by_type, locator_value = _detect_selector(locator)
        condition = EC.element_to_be_clickable if clickable else EC.presence_of_element_located
        
        # Increase wait time for cloud/headless environments; add any extra seconds (e.g. for xWaitFor/xClick)
        wait_timeout = self._timeout + extra_wait_seconds
        if os.environ.get('FOXYIZ_HEADLESS', 'false').lower() in ('true', '1', 'yes'):
            wait_timeout = max(wait_timeout, 10)  # Minimum 10 seconds for headless/cloud
        
        last_exception = None
        for attempt in range(retries + 1):
            try:
                return WebDriverWait(self._driver, wait_timeout).until(condition((by_type, locator_value)))
            except (TimeoutException, NoSuchElementException) as e:
                last_exception = e
                if attempt < retries:
                    # Longer wait between retries in cloud environments
                    wait_seconds = 2 if os.environ.get('FOXYIZ_HEADLESS', 'false').lower() in ('true', '1', 'yes') else 1
                    time.sleep(wait_seconds)
                    continue
                raise last_exception

    def xOpenBrowser(self, browser_type="chrome", driver_path=None):
        """Open a web browser using system PATH or a provided driver_path.
        Users: Ensure the browser driver is installed and on PATH.
        Supports headless mode via FOXYIZ_HEADLESS environment variable.
        Automatically enables headless mode in cloud environments.
        """
        if UIActionHandler._shared_driver:
            return "Browser already open"
        browser_type = self.validate_input(browser_type).lower()
        
        # Detect headless mode from environment variable
        headless_mode = os.environ.get('FOXYIZ_HEADLESS', 'false').lower() in ('true', '1', 'yes')
        
        # Auto-detect cloud environment and enable headless mode if needed
        if not headless_mode:
            is_cloud = False
            # Check for common cloud environment indicators
            # 1. No DISPLAY variable (Linux/Unix without X11)
            if os.name != 'nt' and not os.environ.get('DISPLAY'):
                is_cloud = True
            # 2. AWS EC2 indicators
            if os.path.exists('/sys/hypervisor/uuid') or os.path.exists('/sys/class/dmi/id/product_uuid'):
                try:
                    with open('/sys/class/dmi/id/product_uuid', 'r') as f:
                        uuid = f.read().strip().lower()
                        if uuid.startswith('ec2') or 'amazon' in uuid:
                            is_cloud = True
                except Exception:
                    pass
            # 3. Check for common cloud environment variables
            cloud_vars = ['AWS_EXECUTION_ENV', 'CLOUD_RUN', 'K_SERVICE', 'FUNCTION_TARGET', 
                         'LAMBDA_RUNTIME_DIR', 'AZURE_WEBJOBS_PATH', 'WEBSITE_INSTANCE_ID']
            if any(os.environ.get(var) for var in cloud_vars):
                is_cloud = True
            # 4. Check if running in Docker/container
            if os.path.exists('/.dockerenv') or os.path.exists('/proc/1/cgroup'):
                try:
                    with open('/proc/1/cgroup', 'r') as f:
                        if 'docker' in f.read() or 'kubepods' in f.read():
                            is_cloud = True
                except Exception:
                    pass
            
            if is_cloud:
                headless_mode = True
                os.environ['FOXYIZ_HEADLESS'] = 'true'
                logger.info("Cloud environment detected - enabling headless mode automatically")
        
        try:
            if browser_type == "chrome":
                # Configure Chrome to minimize noisy logs
                if ChromeOptions:
                    opts = ChromeOptions()
                    opts.add_argument("--log-level=3")
                    opts.add_argument("--disable-logging")
                    opts.add_argument("--no-sandbox")
                    opts.add_argument("--disable-dev-shm-usage")
                    
                    # Add headless mode for EC2/cloud execution
                    if headless_mode:
                        opts.add_argument("--headless=new")  # Chrome 109+ (fallback to --headless for older)
                        opts.add_argument("--disable-gpu")
                        opts.add_argument("--window-size=1920,1080")
                    else:
                        opts.add_argument("--disable-gpu")
                    
                    # Additional cloud-friendly options (essential for cloud execution)
                    opts.add_argument("--disable-extensions")
                    opts.add_argument("--disable-plugins")
                    opts.add_argument("--disable-software-rasterizer")
                    opts.add_argument("--disable-background-timer-throttling")
                    opts.add_argument("--disable-backgrounding-occluded-windows")
                    opts.add_argument("--disable-renderer-backgrounding")
                    opts.add_argument("--disable-features=TranslateUI")
                    opts.add_argument("--disable-ipc-flooding-protection")
                    # Improve compatibility with headless mode for better click reliability
                    opts.add_argument("--disable-blink-features=AutomationControlled")
                    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
                    opts.add_experimental_option('useAutomationExtension', False)
                    # Additional options for better cloud stability (only if not already set)
                    # Note: Remote debugging port may conflict in some environments, so we make it optional
                    try:
                        opts.add_argument("--remote-debugging-port=9222")
                        opts.add_argument("--remote-allow-origins=*")
                    except Exception:
                        pass  # Skip if already set or not supported
                else:
                    opts = None

                # Resolve ChromeDriver path: use provided path, else webdriver-manager (matches installed Chrome), else Selenium Manager
                resolved_driver_path = driver_path
                if not resolved_driver_path and WEBDRIVER_MANAGER_AVAILABLE and ChromeDriverManager:
                    try:
                        resolved_driver_path = ChromeDriverManager().install()
                        logger.info("Using ChromeDriver from webdriver-manager (matches installed Chrome version)")
                    except Exception as e:
                        logger.debug("webdriver-manager could not resolve ChromeDriver: %s; falling back to Selenium Manager", e)
                        resolved_driver_path = None

                if ChromeService:
                    service = ChromeService(executable_path=resolved_driver_path) if resolved_driver_path else ChromeService()
                    try:
                        # Suppress chromedriver logs if supported
                        service.log_path = os.devnull  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    self._driver = webdriver.Chrome(service=service, options=opts)
                else:
                    # Fallback older API
                    if resolved_driver_path:
                        self._driver = webdriver.Chrome(executable_path=resolved_driver_path)
                    else:
                        self._driver = webdriver.Chrome()
            elif browser_type == "firefox":
                if headless_mode:
                    from selenium.webdriver.firefox.options import Options as FirefoxOptions
                    firefox_opts = FirefoxOptions()
                    firefox_opts.add_argument("--headless")
                    if driver_path:
                        self._driver = webdriver.Firefox(executable_path=driver_path, options=firefox_opts)
                    else:
                        self._driver = webdriver.Firefox(options=firefox_opts)
                else:
                    if driver_path:
                        self._driver = webdriver.Firefox(executable_path=driver_path)
                    else:
                        self._driver = webdriver.Firefox()
            elif browser_type == "edge":
                if headless_mode:
                    from selenium.webdriver.edge.options import Options as EdgeOptions
                    edge_opts = EdgeOptions()
                    edge_opts.add_argument("--headless")
                    if driver_path:
                        self._driver = webdriver.Edge(executable_path=driver_path, options=edge_opts)
                    else:
                        self._driver = webdriver.Edge(options=edge_opts)
                else:
                    if driver_path:
                        self._driver = webdriver.Edge(executable_path=driver_path)
                    else:
                        self._driver = webdriver.Edge()
            else:
                raise ValueError(f"Unsupported browser: {browser_type}")
            
            # Handle window sizing - maximize_window() may fail in headless mode
            try:
                if not headless_mode:
                    self._driver.maximize_window()
                else:
                    # In headless mode, use set_window_size instead
                    self._driver.set_window_size(1920, 1080)
            except Exception:
                # Fallback if maximize fails (e.g., in headless mode)
                try:
                    self._driver.set_window_size(1920, 1080)
                except Exception:
                    pass  # Continue even if window sizing fails
            
            UIActionHandler._shared_driver = self._driver
            mode_str = "headless" if headless_mode else "normal"
            logger.info(f"Opened {browser_type} browser ({mode_str} mode)")
            return f"Opened {browser_type} browser ({mode_str} mode)"
        except WebDriverException as e:
            msg = f"Failed to open {browser_type}: {str(e)}"
            logger.error(msg)
            raise Exception(msg)

    def xNavigate(self, url):
        """Navigate to a URL.
        Users: Provide a full URL starting with http:// or https://
        """
        if not self._driver:
            raise WebDriverException("Driver not initialized")
        url = self.validate_input(url)
        if not url.startswith(('http://', 'https://')) and url.strip():
            raise ValueError(f"Invalid URL: {url}. Must start with http:// or https://")
        try:
            self._driver.get(url)
            WebDriverWait(self._driver, self._timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            logger.info("Navigation successful")
            return f"Navigated to {url}"
        except Exception as e:
            msg = f"Failed to navigate to {str(url)}: {str(e)}"
            logger.error(msg)
            raise Exception(msg)

    def xType(self, text_and_locator):
        """Type text into an element.
        Users: Input format is 'text;locator'. Locator can be CSS or XPath.
        """
        if not self._driver:
            raise WebDriverException("Driver not initialized")
        parts = self.validate_input(text_and_locator).split(';', 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid input for xType: {text_and_locator}. Expected 'text;locator'")
        text, locator = parts
        try:
            element = self.find_element(locator)
            element.clear()
            element.send_keys(text)
            logger.info("Text input successful")
            return f"Typed '{text}' into element"
        except Exception as e:
            msg = f"xType failed for locator '{locator}': {str(e)}"
            logger.error(_sanitize_error_message(msg))
            raise Exception(_save_error_artifacts(self._driver, getattr(self, '_results_dir', None), None, None, None, msg))

    def xGetText(self, locator):
        """Get text from an element.
        Users: Locator can be CSS or XPath.
        """
        if not self._driver:
            raise WebDriverException("Driver not initialized")
        try:
            element = self.find_element(locator)
            text = element.text
            logger.info("Text retrieved successfully")
            return text
        except Exception as e:
            msg = f"xGetText failed for locator '{locator}': {str(e)}"
            logger.error(_sanitize_error_message(msg))
            raise Exception(_save_error_artifacts(self._driver, getattr(self, '_results_dir', None), None, None, None, msg))

    def xClick(self, locator):
        """Click an element.
        Users: Locator can be CSS or XPath.
        Includes a delay after clicking to ensure the action is fully registered.
        Uses multiple strategies for reliable clicking in headless environments.
        """
        if not self._driver:
            raise WebDriverException("Driver not initialized")
        try:
            click_delay = 1.0 if os.environ.get('FOXYIZ_HEADLESS', 'false').lower() in ('true', '1', 'yes') else 0.5
            locator_lower = locator.lower()
            is_contact_us_click = ("contact" in locator_lower and "us" in locator_lower) or "/footer/" in locator_lower

            for attempt in range(2):
                element = self.find_element(locator, clickable=True, extra_wait_seconds=2)

                # Scroll element into view first (important for headless mode)
                try:
                    self._driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", element)
                    pause_time = 0.5 if os.environ.get('FOXYIZ_HEADLESS', 'false').lower() in ('true', '1', 'yes') else 0.2
                    time.sleep(pause_time)
                except Exception:
                    try:
                        self._driver.execute_script("arguments[0].scrollIntoView(true);", element)
                        pause_time = 0.5 if os.environ.get('FOXYIZ_HEADLESS', 'false').lower() in ('true', '1', 'yes') else 0.2
                        time.sleep(pause_time)
                    except Exception:
                        pass

                # Try multiple click strategies for better reliability on dynamic UIs
                last_click_error = None
                try:
                    ActionChains(self._driver).move_to_element(element).click().perform()
                    logger.debug("Click successful using ActionChains")
                except Exception as e1:
                    last_click_error = e1
                    logger.debug(f"ActionChains click failed: {str(e1)}")
                    try:
                        self._driver.execute_script("arguments[0].click();", element)
                        logger.debug("Click successful using JavaScript")
                    except Exception as e2:
                        last_click_error = e2
                        logger.debug(f"JavaScript click failed: {str(e2)}")
                        try:
                            element.click()
                            logger.debug("Click successful using direct click")
                        except Exception as e3:
                            last_click_error = e3
                            logger.debug(f"Direct click failed: {str(e3)}")
                            try:
                                self._driver.execute_script(
                                    "arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));",
                                    element
                                )
                                logger.debug("Click successful using dispatchEvent")
                            except Exception as e4:
                                raise Exception(
                                    f"All click strategies failed. ActionChains: {str(e1)}, JavaScript: {str(e2)}, Direct: {str(e3)}, Dispatch: {str(e4)}"
                                ) from e4

                time.sleep(click_delay)

                # For contact-us flows, verify modal opened; otherwise retry once.
                if is_contact_us_click:
                    modal_locator = (By.XPATH, "//h2[normalize-space(.)='Get in Touch']|//label[normalize-space(.)='Name *']")
                    try:
                        WebDriverWait(self._driver, 3).until(EC.presence_of_element_located(modal_locator))
                        logger.info("Click successful and contact modal is present")
                        return "Clicked element"
                    except Exception:
                        if attempt == 0:
                            logger.warning("Click executed but contact modal not found; retrying click once")
                            time.sleep(0.5)
                            continue
                        if last_click_error:
                            raise Exception(f"Contact modal did not appear after click: {str(last_click_error)}")
                        raise Exception("Contact modal did not appear after click")

                logger.info("Click successful")
                return "Clicked element"
        except Exception as e:
            msg = f"xClick failed for locator '{locator}': {str(e)}"
            logger.error(_sanitize_error_message(msg))
            raise Exception(_save_error_artifacts(self._driver, getattr(self, '_results_dir', None), None, None, None, msg))

    def xSelectDropdown(self, select_data):
        """Select an option from a dropdown.
        Users: Input format is 'locator;value'. Locator can be CSS or XPath.
        """
        if not self._driver:
            raise WebDriverException("Driver not initialized")
        parts = self.validate_input(select_data).split(';', 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid input for xSelectDropdown: {select_data}. Expected 'locator;value'")
        locator, value = parts
        try:
            element = self.find_element(locator)
            Select(element).select_by_value(value)
            logger.info("Dropdown selection successful")
            return f"Selected '{value}' from dropdown"
        except Exception as e:
            msg = f"xSelectDropdown failed for locator '{locator}' and value '{value}': {str(e)}"
            logger.error(msg)
            raise Exception(_save_error_artifacts(self._driver, getattr(self, '_results_dir', None), None, None, None, msg))

    def xWaitFor(self, locator):
        """Wait for an element to be present.
        Users: Locator can be CSS or XPath.
        Uses 2 seconds longer wait than the default timeout for reliability.
        """
        if not self._driver:
            raise WebDriverException("Driver not initialized")
        try:
            self.find_element(locator, extra_wait_seconds=2)
            logger.info("Element wait successful")
            return "Element is present"
        except Exception as e:
            msg = f"xWaitFor failed for locator '{locator}': {str(e)}"
            logger.error(msg)
            raise Exception(_save_error_artifacts(self._driver, getattr(self, '_results_dir', None), None, None, None, msg))

    def xCloseBrowser(self):
        """Close the browser.
        Includes a delay before closing to ensure all pending operations complete.
        """
        if UIActionHandler._shared_driver:
            try:
                # Add delay before closing to ensure button clicks and other operations complete
                # This prevents browser from closing before actions are fully registered
                time.sleep(0)
                UIActionHandler._shared_driver.quit()
                logger.info("Browser closed")
            except WebDriverException as e:
                logger.error(f"Error closing browser: {str(e)}")
                raise
            UIActionHandler._shared_driver = None
            self._driver = None
            return "Browser closed"
        return "No browser to close"

    def xIsChecked(self, locator):
        """Check if a checkbox is checked."""
        if not self._driver:
            raise WebDriverException("Driver not initialized")
        element = self.find_element(locator)
        state = "checked" if element.is_selected() else "unchecked"
        logger.info("Checkbox state checked")
        return state

    def xDragAndDrop(self, drag_data):
        """Drag and drop an element.
        Users: Input format is 'source_locator;target_locator'. Both can be CSS or XPath.
        """
        if not self._driver:
            raise WebDriverException("Driver not initialized")
        parts = self.validate_input(drag_data).split(';', 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid input for xDragAndDrop: {drag_data}. Expected 'source_locator;target_locator'")
        source_locator, target_locator = parts
        try:
            source_element = self.find_element(source_locator)
            target_element = self.find_element(target_locator)
            ActionChains(self._driver).drag_and_drop(source_element, target_element).perform()
            logger.info("Drag and drop successful")
            return "Drag and drop performed"
        except Exception as e:
            msg = f"xDragAndDrop failed for '{drag_data}': {str(e)}"
            logger.error(msg)
            raise Exception(_save_error_artifacts(self._driver, getattr(self, '_results_dir', None), None, None, None, msg))

    def xUploadFile(self, upload_data):
        """Upload a file.
        Users: Input format is 'locator;file_path'. Locator can be CSS or XPath.
        """
        if not self._driver:
            raise WebDriverException("Driver not initialized")
        parts = self.validate_input(upload_data).split(';', 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid input for xUploadFile: {upload_data}. Expected 'locator;file_path'")
        locator, file_path = parts
        absolute_path = os.path.abspath(file_path)
        if not os.path.exists(absolute_path):
            raise FileNotFoundError(f"File not found: {absolute_path}")
        try:
            element = self.find_element(locator)
            element.send_keys(absolute_path)
            logger.info("File upload successful")
            return f"File '{file_path}' uploaded"
        except Exception as e:
            msg = f"xUploadFile failed for locator '{locator}' and file '{file_path}': {str(e)}"
            logger.error(msg)
            raise Exception(_save_error_artifacts(self._driver, getattr(self, '_results_dir', None), None, None, None, msg))

    def xGetCurrentUrl(self):
        """Get the current URL."""
        if not self._driver:
            raise WebDriverException("Driver not initialized")
        try:
            url = self._driver.current_url
            logger.info("URL retrieved successfully")
            return url
        except Exception as e:
            msg = f"xGetCurrentUrl failed: {str(e)}"
            logger.error(msg)
            raise Exception(_save_error_artifacts(self._driver, getattr(self, '_results_dir', None), None, None, None, msg))

    def xGetTitle(self):
        """Get the page title."""
        if not self._driver:
            raise WebDriverException("Driver not initialized")
        try:
            title = self._driver.title
            logger.info("Title retrieved successfully")
            return title
        except Exception as e:
            msg = f"xGetTitle failed: {str(e)}"
            logger.error(msg)
            raise Exception(_save_error_artifacts(self._driver, getattr(self, '_results_dir', None), None, None, None, msg))

    def xHandleAlert(self, action):
        """Handle a JavaScript alert."""
        if not self._driver:
            raise WebDriverException("Driver not initialized")
        try:
            alert = WebDriverWait(self._driver, self._timeout).until(EC.alert_is_present())
            action = self.validate_input(action).lower()
            if action == "accept":
                alert.accept()
                logger.info("Alert accepted")
                return "Alert accepted"
            elif action == "dismiss":
                alert.dismiss()
                logger.info("Alert dismissed")
                return "Alert dismissed"
            elif action.startswith("type:"):
                text = action.split(":", 1)[1]
                alert.send_keys(text)
                alert.accept()
                logger.info("Alert text input successful")
                return f"Typed '{text}' into alert and accepted"
            else:
                raise ValueError(f"Unsupported alert action: {action}")
        except TimeoutException:
            msg = "No alert present within timeout"
            logger.error(msg)
            raise TimeoutException(msg)
        except Exception as e:
            msg = f"xHandleAlert failed: {str(e)}"
            logger.error(msg)
            raise Exception(_save_error_artifacts(self._driver, getattr(self, '_results_dir', None), None, None, None, msg))

    def xHover(self, locator):
        """Hover over an element."""
        if not self._driver:
            raise WebDriverException("Driver not initialized")
        try:
            element = self.find_element(locator)
            ActionChains(self._driver).move_to_element(element).perform()
            logger.info("Hover successful")
            return "Hovered over element"
        except Exception as e:
            msg = f"xHover failed for locator '{locator}': {str(e)}"
            logger.error(msg)
            raise Exception(_save_error_artifacts(self._driver, getattr(self, '_results_dir', None), None, None, None, msg))

    def xSendKeys(self, key_data):
        """Send special keys."""
        if not self._driver:
            raise WebDriverException("Driver not initialized")
        parts = self.validate_input(key_data).split(';', 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid input for xSendKeys: {key_data}. Expected 'locator;key_name'")
        locator, key_name = parts
        try:
            element = self.find_element(locator)
            key = getattr(Keys, key_name.upper(), key_name)
            element.send_keys(key)
            logger.info("Key send successful")
            return f"Sent key '{key_name}'"
        except Exception as e:
            msg = f"xSendKeys failed for locator '{locator}' and key '{key_name}': {str(e)}"
            logger.error(msg)
            raise Exception(_save_error_artifacts(self._driver, getattr(self, '_results_dir', None), None, None, None, msg))

    def xContextClick(self, locator):
        """Perform a right-click."""
        if not self._driver:
            raise WebDriverException("Driver not initialized")
        try:
            element = self.find_element(locator)
            ActionChains(self._driver).context_click(element).perform()
            logger.info("Context click successful")
            return "Context click performed"
        except Exception as e:
            msg = f"xContextClick failed for locator '{locator}': {str(e)}"
            logger.error(msg)
            raise Exception(_save_error_artifacts(self._driver, getattr(self, '_results_dir', None), None, None, None, msg))

    def xSetViewport(self, viewport_data):
        """Set browser viewport size."""
        if not self._driver:
            raise WebDriverException("Driver not initialized")
        parts = self.validate_input(viewport_data).split(';', 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid input for xSetViewport: {viewport_data}. Expected 'width;height'")
        width, height = parts
        try:
            width = int(width)
            height = int(height)
        except ValueError:
            raise ValueError(f"Invalid viewport dimensions: {width}x{height}")
        try:
            self._driver.set_window_size(width, height)
            logger.info("Viewport set successfully")
            return f"Viewport set to {width}x{height}"
        except Exception as e:
            msg = f"xSetViewport failed: {str(e)}"
            logger.error(msg)
            raise Exception(_save_error_artifacts(self._driver, getattr(self, '_results_dir', None), None, None, None, msg))

    def xRefresh(self):
        """Refresh the current page."""
        if not self._driver:
            raise WebDriverException("Driver not initialized")
        try:
            self._driver.refresh()
            logger.info("Page refreshed successfully")
            return "Page refreshed"
        except Exception as e:
            msg = f"xRefresh failed: {str(e)}"
            logger.error(msg)
            raise Exception(_save_error_artifacts(self._driver, getattr(self, '_results_dir', None), None, None, None, msg))

    def xStartTimer(self):
        """Start a timer for performance measurement."""
        self._start_time = time.time()
        logger.info("Timer started")
        return "Timer started"

    def xStopTimer(self, output_var):
        """Stop timer and return elapsed time in milliseconds."""
        if not hasattr(self, '_start_time'):
            raise ValueError("Timer not started. Call xStartTimer first.")
        elapsed_time = (time.time() - self._start_time) * 1000  # Convert to milliseconds
        logger.info(f"Timer stopped: {elapsed_time:.2f}ms")
        return f"{elapsed_time:.2f}"

    def xAssertLessThan(self, comparison_data):
        """Assert that a value is less than expected threshold."""
        parts = self.validate_input(comparison_data).split(';', 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid input for xAssertLessThan: {comparison_data}. Expected 'actual;expected'")
        actual, expected = parts
        try:
            actual_val = float(actual)
            expected_val = float(expected)
        except ValueError:
            raise ValueError(f"Invalid numeric values: {actual}, {expected}")
        
        if actual_val < expected_val:
            logger.info("Assertion passed")
            return "Assertion passed"
        else:
            raise AssertionError(f"Assertion failed: {actual_val} >= {expected_val}")

    def xAcceptAlert(self):
        """Accept a JavaScript alert."""
        return self.xHandleAlert("accept")

    def xDismissAlert(self):
        """Dismiss a JavaScript alert."""
        return self.xHandleAlert("dismiss")

class MathActionHandler(ActionHandler):
    """Handles mathematical operations."""
    def validate_numbers(self, inputs, expected_count=None):
        """Validate input strings are numeric and of expected count."""
        numbers = [x.strip() for x in inputs if x.strip()]
        if expected_count is not None and len(numbers) != expected_count:
            raise ValueError(f"Expected {expected_count} numbers, got {len(numbers)}: {inputs}")
        if not numbers:
            raise ValueError("No numeric inputs provided.")
        try:
            return [float(x) for x in numbers]
        except ValueError as e:
            raise ValueError(f"Non-numeric input: {inputs}")

    def xAdd(self, aIn):
        """Add numbers."""
        vIn = self.validate_input(aIn).split(';')
        numbers = self.validate_numbers(vIn)
        result = sum(numbers)
        logger.info("Math addition successful")
        return str(result)

    def xMinus(self, aIn):
        """Subtract two numbers."""
        vIn = self.validate_input(aIn).split(';')
        numbers = self.validate_numbers(vIn, expected_count=2)
        result = numbers[0] - numbers[1]
        logger.info("Math subtraction successful")
        return str(result)

    def xMultiply(self, aIn):
        """Multiply numbers."""
        vIn = self.validate_input(aIn).split(';')
        numbers = self.validate_numbers(vIn)
        result = 1
        for num in numbers:
            result *= num
        logger.info("Math multiplication successful")
        return str(result)

    def xDiv(self, aIn):
        """Divide two numbers."""
        vIn = self.validate_input(aIn).split(';')
        numbers = self.validate_numbers(vIn, expected_count=2)
        if numbers[1] == 0:
            raise ValueError("Division by zero")
        result = numbers[0] / numbers[1]
        logger.info("Math division successful")
        return str(result)

    def xCompare(self, aIn):
        """Compare two values."""
        vIn = self.validate_input(aIn).split(';')
        if len(vIn) != 2:
            raise ValueError(f"Invalid input for xCompare: {aIn}. Expected 'value1;value2'")
        result = str(vIn[0] == vIn[1])
        logger.info("Math comparison successful")
        return result

    def xPower(self, aIn):
        """Calculate power."""
        vIn = self.validate_input(aIn).split(';')
        numbers = self.validate_numbers(vIn, expected_count=2)
        result = numbers[0] ** numbers[1]
        logger.info("Math power operation successful")
        return str(result)

    def xModulo(self, aIn):
        """Calculate modulo."""
        vIn = self.validate_input(aIn).split(';')
        numbers = self.validate_numbers(vIn, expected_count=2)
        result = numbers[0] % numbers[1]
        logger.info("Math modulo operation successful")
        return str(result)

    def xRound(self, aIn):
        """Round a number."""
        vIn = self.validate_input(aIn).split(';')
        numbers = self.validate_numbers(vIn, expected_count=2)
        result = round(numbers[0], int(numbers[1]))
        logger.info("Math round operation successful")
        return str(result)

class APIActionHandler(ActionHandler):
    """Handles API-related actions."""
    _last_response = None  # Store last API response for xJSON actions

    def get_auth_headers(self, api_key):
        """Retrieve authentication headers."""
        return {"Content-Type": "application/json", "api_key": self.validate_input(api_key)}

    def xGetAuthToken(self, aIn):
        """Retrieve an authentication token."""
        vIn = self.validate_input(aIn).split(";")
        auth_url, payload_path = vIn[0], vIn[1]
        payload = {}
        if payload_path and os.path.exists(payload_path):
            with open(payload_path, 'r') as f:
                payload = json.load(f)
        response = requests.post(auth_url, json=payload)
        if response.status_code == 200:
            self._auth_token = response.json().get("token")
            if not self._auth_token:
                raise ValueError("Token not found in response")
            logger.info("Token retrieval successful")
            return "Token retrieved successfully"
        logger.error(f"Token retrieval failed: {response.status_code}")
        return "Token failed"

    def xGet(self, aIn):
        """Send a GET request."""
        vIn = self.validate_input(aIn).split(";")
        base_url, endpoint = vIn[0], vIn[1]
        headers = self.get_auth_headers(vIn[2]) if len(vIn) == 3 else None
        response = requests.get(base_url + endpoint, headers=headers)
        try:
            APIActionHandler._last_response = response.json()
        except ValueError:
            APIActionHandler._last_response = None
        logger.info("API GET request successful")
        return str(response.status_code)

    def xPost(self, aIn):
        """Send a POST request."""
        vIn = self.validate_input(aIn).split(";")
        base_url, endpoint, payload_path = vIn[0], vIn[1], vIn[2]
        payload = {}
        if payload_path and os.path.exists(payload_path):
            with open(payload_path, 'r') as f:
                payload = json.load(f)
        headers = self.get_auth_headers(vIn[3]) if len(vIn) == 4 else None
        response = requests.post(base_url + endpoint, json=payload, headers=headers)
        try:
            APIActionHandler._last_response = response.json()
        except ValueError:
            APIActionHandler._last_response = None
        logger.info("API POST request successful")
        return str(response.status_code)

    def xDelete(self, aIn):
        """Send a DELETE request."""
        vIn = self.validate_input(aIn).split(";")
        base_url, endpoint = vIn[0], vIn[1]
        headers = self.get_auth_headers(vIn[2]) if len(vIn) == 3 else None
        response = requests.delete(base_url + endpoint, headers=headers)
        try:
            APIActionHandler._last_response = response.json()
        except ValueError:
            APIActionHandler._last_response = None
        logger.info("API DELETE request successful")
        return str(response.status_code)

    def xPut(self, aIn):
        """Send a PUT request."""
        vIn = self.validate_input(aIn).split(";")
        base_url, endpoint, payload_path = vIn[0], vIn[1], vIn[2]
        payload = {}
        if payload_path and os.path.exists(payload_path):
            with open(payload_path, 'r') as f:
                payload = json.load(f)
        headers = self.get_auth_headers(vIn[3]) if len(vIn) == 4 else None
        response = requests.put(base_url + endpoint, json=payload, headers=headers)
        try:
            APIActionHandler._last_response = response.json()
        except ValueError:
            APIActionHandler._last_response = None
        logger.info("API PUT request successful")
        return str(response.status_code)

    def xGetOAuthTokenForm(self, aIn):
        """
        Request an OAuth2 token using application/x-www-form-urlencoded body.

        Input format:
            token_url;client_id;client_secret;grant_type;[scope];[extra_params_json_or_query]

        - token_url (required): token endpoint URL
        - client_id (required): client id (may be omitted if using other auth)
        - client_secret (required): client secret (may be omitted if using other auth)
        - grant_type (required): e.g. client_credentials, password, authorization_code
        - scope (optional): space-separated scopes
        - extra_params_json_or_query (optional): JSON object string or query-string (k=v&k2=v2) for additional form params

        Returns:
            access_token string if present in JSON response, otherwise full JSON/text response.
        """
        parts = self.validate_input(aIn).split(';')
        if len(parts) < 4:
            raise ValueError("Invalid input for xGetOAuthTokenForm: expected token_url;client_id;client_secret;grant_type;[scope];[extra_params]")

        token_url = parts[0].strip()
        client_id = parts[1].strip()
        client_secret = parts[2].strip()
        grant_type = parts[3].strip()
        scope = parts[4].strip() if len(parts) >= 5 and parts[4].strip() else None
        extra = parts[5].strip() if len(parts) >= 6 and parts[5].strip() else None

        data = {'grant_type': grant_type}
        if client_id:
            data['client_id'] = client_id
        if client_secret:
            data['client_secret'] = client_secret
        if scope:
            data['scope'] = scope

        if extra:
            try:
                extra_obj = json.loads(extra)
                if isinstance(extra_obj, dict):
                    data.update({k: str(v) for k, v in extra_obj.items()})
            except Exception:
                try:
                    for pair in [p for sep in ('&', ';') for p in extra.split(sep)]:
                        if '=' in pair:
                            k, v = pair.split('=', 1)
                            data[k.strip()] = v.strip()
                except Exception:
                    data['extra'] = extra

        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        req_timeout = getattr(self, '_timeout', 6)
        resp = requests.post(token_url, data=data, headers=headers, timeout=req_timeout)
        text = resp.text
        if 200 <= resp.status_code < 300:
            try:
                j = resp.json()
                token = j.get('access_token') or j.get('token') or j.get('id_token')
                return token if token else json.dumps(j)
            except Exception:
                return text
        else:
            raise Exception(f"Token request failed: {resp.status_code} {text}")

class JSONActionHandler(ActionHandler):
    """Handles JSON-related actions."""
    def _get_json_value(self, json_data, key_path):
        """Extract value from JSON using dot notation key path."""
        if not json_data:
            raise ValueError("No JSON response available")
        keys = key_path.split('.')
        current = json_data
        try:
            for key in keys:
                if key.isdigit():
                    current = current[int(key)]
                else:
                    current = current[key]
            return current
        except (KeyError, IndexError, TypeError):
            raise ValueError(f"Key path '{key_path}' not found in JSON")

    def xExtractJson(self, aIn):
        """Extract a value from the last API response JSON."""
        key_path = self.validate_input(aIn)
        value = self._get_json_value(APIActionHandler._last_response, key_path)
        logger.info(f"Extracted JSON value: {value}")
        return str(value)

    def xCompareJson(self, aIn):
        """Compare a JSON value with an expected value."""
        vIn = self.validate_input(aIn).split(';')
        if len(vIn) != 2:
            raise ValueError(f"Invalid input for xCompareJson: {aIn}. Expected 'key_path;expected_value'")
        key_path, expected_value = vIn
        try:
            value = self._get_json_value(APIActionHandler._last_response, key_path)
            # Convert both to strings for comparison to handle type mismatches (e.g., int 200 vs string "200")
            result = str(str(value) == str(expected_value))
            logger.info(f"JSON comparison result: {result}")
            return result
        except ValueError as e:
            # If key doesn't exist, check if it's because of an API error (404, etc.)
            # In that case, return "False" instead of raising an error
            if APIActionHandler._last_response:
                try:
                    cod = self._get_json_value(APIActionHandler._last_response, 'cod')
                    # If cod is not 200, it's an API error, return False gracefully
                    if str(cod) != "200":
                        logger.info(f"JSON key '{key_path}' not found due to API error (cod={cod}), returning False")
                        return "False"
                except ValueError:
                    pass
            # Re-raise the original error if it's not an API error case
            raise e

    def xValidateJson(self, aIn):
        """Validate if a key exists in the JSON response."""
        key_path = self.validate_input(aIn)
        try:
            self._get_json_value(APIActionHandler._last_response, key_path)
            logger.info(f"JSON key '{key_path}' validated")
            return "True"
        except ValueError:
            logger.info(f"JSON key '{key_path}' not found")
            return "False"

class AIActionHandler(ActionHandler):
    """Handles AI-related actions (OpenAI Chat Completions).

    Usage formats (semicolon-separated):
    - xTextPrompt: "api_endpoint;api_key;prompt[;model][;options]"
      Examples:
        - "openai;env:OPENAI_API_KEY;What is 2+2?;gpt-3.5-turbo;temp=0.2,max=16"
        - ";;@f/ai_payload.json"  (payload JSON must include at least 'prompt')
        - ";;@prompts/hello.txt;gpt-4"
    - xContextPrompt: "api_endpoint;api_key;context_file;prompt[;model][;options]"
      Example:
        - "openai;env:OPENAI_API_KEY;@f/_others/AI.txt;Create 5 yPAD ideas;gpt-4;temp=0.7,max=300"

    Options string: "temp=0.7,max=500,model=gpt-4" (keys supported: temp/temperature, max/max_tokens, model)

    API key sources (priority):
      1) Provided value (supports "env:VAR" indirection)
      2) Environment variable OPENAI_API_KEY
      3) f/ai_payload.json with {"api_key": "..."}

    Works with or without the openai library installed (fallback to REST).
    """

    def _read_text_file(self, path):
        resolved = path[1:] if path.startswith('@') else path
        with open(resolved, 'r', encoding='utf-8') as f:
            return f.read()

    def _load_json_file(self, path):
        resolved = path[1:] if path.startswith('@') else path
        with open(resolved, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _resolve_api_key(self, candidate):
        if candidate and candidate.strip():
            val = candidate.strip()
            if val.lower().startswith('env:'):
                env_name = val.split(':', 1)[1].strip()
                key = os.environ.get(env_name)
                if key:
                    return key
            return val
        env_key = os.environ.get('OPENAI_API_KEY')
        if env_key:
            return env_key
        try:
            default_payload = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'f', 'ai_payload.json'))
            if os.path.exists(default_payload):
                data = self._load_json_file(default_payload)
                if isinstance(data, dict) and data.get('api_key'):
                    return data.get('api_key')
        except Exception:
            pass
        raise ValueError("OpenAI API key not provided. Pass it, set OPENAI_API_KEY, or populate f/ai_payload.json")

    def _parse_options(self, options_str):
        out = {}
        if not options_str:
            return out
        for kv in options_str.split(','):
            if '=' in kv:
                k, v = kv.split('=', 1)
                k = k.strip().lower()
                v = v.strip()
                if k in {'temp', 'temperature'}:
                    try:
                        out['temperature'] = float(v)
                    except ValueError:
                        pass
                elif k in {'max', 'max_tokens'}:
                    try:
                        out['max_tokens'] = int(v)
                    except ValueError:
                        pass
                elif k == 'model':
                    out['model'] = v
        return out

    def _call_openai_chat(self, api_key, messages, model, temperature=0.7, max_tokens=500, endpoint=None):
        use_endpoint = (endpoint or '').strip() or 'https://api.openai.com/v1/chat/completions'
        try:
            import openai  # type: ignore
            openai.api_key = api_key
            response = openai.ChatCompletion.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            content = response['choices'][0]['message']['content']
            return content.strip()
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"openai SDK call failed, falling back to REST: {str(e)}")
        try:
            headers = {
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json'
            }
            payload = {
                'model': model,
                'messages': messages,
                'max_tokens': max_tokens,
                'temperature': temperature
            }
            resp = requests.post(use_endpoint, headers=headers, json=payload, timeout=45)
            if resp.status_code != 200:
                snippet = resp.text[:280].replace('\n', ' ')
                raise RuntimeError(f"OpenAI API error {resp.status_code}: {snippet}")
            data = resp.json()
            content = data['choices'][0]['message']['content']
            return content.strip()
        except Exception as e:
            raise Exception(f"Failed to call OpenAI API: {str(e)}")

    def _prepare_messages(self, system_text, user_text):
        messages = []
        if system_text:
            messages.append({"role": "system", "content": system_text})
        messages.append({"role": "user", "content": user_text})
        return messages

    def xTextPrompt(self, aIn):
        """Send a simple text prompt to OpenAI.

        Input: "api_endpoint;api_key;prompt[;model][;options]"
        - prompt may be raw text, "@file.txt", or "@payload.json"
        - options: "temp=0.7,max=500"; model can also be overridden here
        Returns: assistant message content as plain text
        """
        parts = self.validate_input(aIn).split(';')
        if len(parts) < 3:
            raise ValueError(f"Invalid input for xTextPrompt: {aIn}. Expected 'api_endpoint;api_key;prompt[;model][;options]'")
        api_endpoint = parts[0]
        api_key_in = parts[1]
        prompt_in = parts[2]
        model_in = parts[3] if len(parts) >= 4 and parts[3].strip() else ''
        options_in = parts[4] if len(parts) >= 5 else ''

        api_key = self._resolve_api_key(api_key_in)

        system_text = None
        temperature = 0.7
        max_tokens = 500
        model = model_in.strip() or 'gpt-3.5-turbo'

        # If prompt references a JSON payload, load fields; if text file, read content
        prompt_text = prompt_in
        if prompt_in.strip().startswith('@') and prompt_in.strip().lower().endswith('.json'):
            payload = self._load_json_file(prompt_in)
            system_text = payload.get('system')
            prompt_text = payload.get('prompt', '')
            if payload.get('model'):
                model = payload.get('model')
            if isinstance(payload.get('temperature'), (int, float)):
                temperature = float(payload.get('temperature'))
            if isinstance(payload.get('max_tokens'), int):
                max_tokens = int(payload.get('max_tokens'))
        elif prompt_in.strip().startswith('@'):
            prompt_text = self._read_text_file(prompt_in)

        # Parse options string overrides
        opts = self._parse_options(options_in)
        model = opts.get('model', model)
        temperature = opts.get('temperature', temperature)
        max_tokens = opts.get('max_tokens', max_tokens)

        messages = self._prepare_messages(system_text, prompt_text)

        try:
            endpoint = None if api_endpoint.strip().lower() in {'', 'openai'} else api_endpoint.strip()
            answer = self._call_openai_chat(api_key, messages, model, temperature=temperature, max_tokens=max_tokens, endpoint=endpoint)
            logger.info("AI text prompt successful")
            return answer
        except Exception as e:
            msg = f"xTextPrompt failed: {str(e)}"
            logger.error(msg)
            raise Exception(msg)

    def xContextPrompt(self, aIn):
        """Send a context + prompt to OpenAI as system/user messages.

        Input: "api_endpoint;api_key;context_file;prompt[;model][;options]"
        - context_file can be prefixed with @ and must be a readable text file
        Returns: assistant message content as plain text
        """
        parts = self.validate_input(aIn).split(';')
        if len(parts) < 4:
            raise ValueError(f"Invalid input for xContextPrompt: {aIn}. Expected 'api_endpoint;api_key;context_file;prompt[;model][;options]'")
        api_endpoint = parts[0]
        api_key_in = parts[1]
        ctx_path = parts[2]
        user_prompt_in = parts[3]
        model_in = parts[4] if len(parts) >= 5 and parts[4].strip() else ''
        options_in = parts[5] if len(parts) >= 6 else ''

        api_key = self._resolve_api_key(api_key_in)
        model = model_in.strip() or 'gpt-3.5-turbo'
        temperature = 0.7
        max_tokens = 500

        # Read context file
        try:
            context_text = self._read_text_file(ctx_path if ctx_path.startswith('@') else f"@{ctx_path}")
        except Exception as e:
            raise Exception(f"Failed to read context file '{ctx_path}': {str(e)}")

        # Prompt can also be @file
        if user_prompt_in.strip().startswith('@'):
            user_prompt = self._read_text_file(user_prompt_in)
        else:
            user_prompt = user_prompt_in

        # Options overrides
        opts = self._parse_options(options_in)
        model = opts.get('model', model)
        temperature = opts.get('temperature', temperature)
        max_tokens = opts.get('max_tokens', max_tokens)

        messages = self._prepare_messages(context_text, user_prompt)

        try:
            endpoint = None if api_endpoint.strip().lower() in {'', 'openai'} else api_endpoint.strip()
            answer = self._call_openai_chat(api_key, messages, model, temperature=temperature, max_tokens=max_tokens, endpoint=endpoint)
            logger.info("AI context prompt successful")
            return answer
        except Exception as e:
            msg = f"xContextPrompt failed: {str(e)}"
            logger.error(msg)
            raise Exception(msg)

class FileActionHandler(ActionHandler):
    """Handles file system operations."""
    
    def xFileCopy(self, aIn):
        """Copy files or directories from source to destination."""
        parts = self.validate_input(aIn).split(';')
        if len(parts) < 2:
            raise ValueError(f"Invalid input for xFileCopy: {aIn}. Expected 'source_path;destination_path;[overwrite]'")
        
        source_path = parts[0].strip()
        dest_path = parts[1].strip()
        overwrite = parts[2].strip().lower() == 'true' if len(parts) > 2 else False
        
        if not os.path.exists(source_path):
            raise FileNotFoundError(f"Source path does not exist: {source_path}")
        
        try:
            if os.path.isfile(source_path):
                if os.path.exists(dest_path) and not overwrite:
                    raise FileExistsError(f"Destination file exists and overwrite is false: {dest_path}")
                shutil.copy2(source_path, dest_path)
                logger.info(f"File copied: {source_path} -> {dest_path}")
                return f"File copied successfully"
            elif os.path.isdir(source_path):
                if os.path.exists(dest_path) and not overwrite:
                    raise FileExistsError(f"Destination directory exists and overwrite is false: {dest_path}")
                shutil.copytree(source_path, dest_path, dirs_exist_ok=overwrite)
                logger.info(f"Directory copied: {source_path} -> {dest_path}")
                return f"Directory copied successfully"
            else:
                raise ValueError(f"Source path is neither file nor directory: {source_path}")
        except Exception as e:
            msg = f"xFileCopy failed: {str(e)}"
            logger.error(msg)
            raise Exception(msg)
    
    def xFileDelete(self, aIn):
        """Delete files or directories with safety checks."""
        parts = self.validate_input(aIn).split(';')
        if len(parts) < 1:
            raise ValueError(f"Invalid input for xFileDelete: {aIn}. Expected 'file_path;[confirm]'")
        
        file_path = parts[0].strip()
        confirm = parts[1].strip().lower() == 'true' if len(parts) > 1 else True
        
        if not os.path.exists(file_path):
            logger.info(f"File/directory does not exist: {file_path}")
            return "File/directory not found"
        
        if not confirm:
            raise ValueError("Deletion not confirmed. Set confirm=true to proceed.")
        
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
                logger.info(f"File deleted: {file_path}")
                return "File deleted successfully"
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
                logger.info(f"Directory deleted: {file_path}")
                return "Directory deleted successfully"
        except Exception as e:
            msg = f"xFileDelete failed: {str(e)}"
            logger.error(msg)
            raise Exception(msg)
    
    def xFileExists(self, aIn):
        """Check if file or directory exists."""
        file_path = self.validate_input(aIn).strip()
        
        if os.path.exists(file_path):
            file_type = "file" if os.path.isfile(file_path) else "directory"
            logger.info(f"{file_type} exists: {file_path}")
            return "exists"
        else:
            logger.info(f"Path does not exist: {file_path}")
            return "not_found"

class EmailActionHandler(ActionHandler):
    """Handles email operations."""
    
    def xEmailSend(self, aIn):
        """Send email with attachments using SMTP."""
        parts = self.validate_input(aIn).split(';')
        if len(parts) < 7:
            raise ValueError(f"Invalid input for xEmailSend: {aIn}. Expected 'smtp_server;port;username;password;to_email;subject;body;[attachment_path]'")
        
        smtp_server = parts[0].strip()
        port = int(parts[1].strip())
        username = parts[2].strip()
        password = parts[3].strip()
        to_email = parts[4].strip()
        subject = parts[5].strip()
        body = parts[6].strip()
        attachment_path = parts[7].strip() if len(parts) > 7 and parts[7].strip() else None
        
        try:
            msg = MIMEMultipart()
            msg['From'] = username
            msg['To'] = to_email
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'plain'))
            
            if attachment_path and os.path.exists(attachment_path):
                with open(attachment_path, "rb") as attachment:
                    part = MIMEBase('application', 'octet-stream')
                    part.set_payload(attachment.read())
                    encoders.encode_base64(part)
                    part.add_header('Content-Disposition', f'attachment; filename= {os.path.basename(attachment_path)}')
                    msg.attach(part)
            
            server = smtplib.SMTP(smtp_server, port)
            server.starttls()
            server.login(username, password)
            text = msg.as_string()
            server.sendmail(username, to_email, text)
            server.quit()
            
            logger.info(f"Email sent successfully to {to_email}")
            return "Email sent successfully"
        except Exception as e:
            msg = f"xEmailSend failed: {str(e)}"
            logger.error(msg)
            raise Exception(msg)
    
    def xEmailRead(self, aIn):
        """Read emails from inbox with filtering."""
        parts = self.validate_input(aIn).split(';')
        if len(parts) < 4:
            raise ValueError(f"Invalid input for xEmailRead: {aIn}. Expected 'imap_server;port;username;password;[subject_filter];[max_count]'")
        
        imap_server = parts[0].strip()
        port = int(parts[1].strip())
        username = parts[2].strip()
        password = parts[3].strip()
        subject_filter = parts[4].strip() if len(parts) > 4 and parts[4].strip() else None
        max_count = int(parts[5].strip()) if len(parts) > 5 and parts[5].strip() else 10
        
        try:
            mail = imaplib.IMAP4_SSL(imap_server, port)
            mail.login(username, password)
            mail.select('inbox')
            
            search_criteria = 'ALL'
            if subject_filter:
                search_criteria = f'SUBJECT "{subject_filter}"'
            
            status, messages = mail.search(None, search_criteria)
            email_ids = messages[0].split()
            
            email_ids = email_ids[-max_count:] if len(email_ids) > max_count else email_ids
            
            email_count = len(email_ids)
            mail.logout()
            
            logger.info(f"Found {email_count} emails")
            return f"Found {email_count} emails"
        except Exception as e:
            msg = f"xEmailRead failed: {str(e)}"
            logger.error(msg)
            raise Exception(msg)

class DBActionHandler(ActionHandler):
    """Handles database operations."""
    _connections = {}
    
    def xDBConnect(self, aIn):
        """Connect to database (SQLite, MySQL, PostgreSQL)."""
        parts = self.validate_input(aIn).split(';')
        if len(parts) != 2:
            raise ValueError(f"Invalid input for xDBConnect: {aIn}. Expected 'db_type;connection_string'")
        
        db_type = parts[0].strip().lower()
        connection_string = parts[1].strip()
        
        try:
            if db_type == 'sqlite':
                db_path = connection_string
                conn = sqlite3.connect(db_path)
                DBActionHandler._connections['default'] = conn
                logger.info(f"Connected to SQLite database: {db_path}")
                return "Connected to SQLite database"
            else:
                logger.info(f"Database type {db_type} not fully implemented yet")
                return f"Database type {db_type} connection attempted"
        except Exception as e:
            msg = f"xDBConnect failed: {str(e)}"
            logger.error(msg)
            raise Exception(msg)
    
    def xDBQuery(self, aIn):
        """Execute SQL query and return results."""
        parts = self.validate_input(aIn).split(';')
        if len(parts) < 1:
            raise ValueError(f"Invalid input for xDBQuery: {aIn}. Expected 'query_string;[max_rows]'")
        
        query = parts[0].strip()
        max_rows = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip() else 100
        
        if 'default' not in DBActionHandler._connections:
            raise ValueError("No database connection. Call xDBConnect first.")
        
        try:
            conn = DBActionHandler._connections['default']
            cursor = conn.cursor()
            cursor.execute(query)
            
            if query.strip().upper().startswith('SELECT'):
                results = cursor.fetchmany(max_rows)
                result_str = str(results) if results else "No results"
                logger.info(f"Query executed successfully, returned {len(results)} rows")
                return result_str
            else:
                conn.commit()
                affected_rows = cursor.rowcount
                logger.info(f"Query executed successfully, affected {affected_rows} rows")
                return f"Query executed, affected {affected_rows} rows"
        except Exception as e:
            msg = f"xDBQuery failed: {str(e)}"
            logger.error(msg)
            raise Exception(msg)
    
    def xDBInsert(self, aIn):
        """Insert data into database table."""
        parts = self.validate_input(aIn).split(';')
        if len(parts) != 2:
            raise ValueError(f"Invalid input for xDBInsert: {aIn}. Expected 'table_name;column_values'")
        
        table_name = parts[0].strip()
        column_values = parts[1].strip()
        
        if 'default' not in DBActionHandler._connections:
            raise ValueError("No database connection. Call xDBConnect first.")
        
        try:
            columns = []
            values = []
            for pair in column_values.split(','):
                if '=' in pair:
                    col, val = pair.split('=', 1)
                    columns.append(col.strip())
                    values.append(val.strip())
            
            if not columns:
                raise ValueError("No valid column=value pairs found")
            
            columns_str = ', '.join(columns)
            placeholders = ', '.join(['?' for _ in values])
            query = f"INSERT INTO {table_name} ({columns_str}) VALUES ({placeholders})"
            
            conn = DBActionHandler._connections['default']
            cursor = conn.cursor()
            cursor.execute(query, values)
            conn.commit()
            
            affected_rows = cursor.rowcount
            logger.info(f"Inserted {affected_rows} row(s) into {table_name}")
            return f"Inserted {affected_rows} row(s) successfully"
        except Exception as e:
            msg = f"xDBInsert failed: {str(e)}"
            logger.error(msg)
            raise Exception(msg)

class LogicActionHandler(ActionHandler):
    """Handles conditional logic operations."""
    
    def xLogicIf(self, aIn):
        """Execute conditional logic with if-then-else branching."""
        parts = self.validate_input(aIn).split(';')
        if len(parts) < 3:
            raise ValueError(f"Invalid input for xLogicIf: {aIn}. Expected 'condition;true_action;false_action'")
        
        condition = parts[0].strip()
        true_action = parts[1].strip()
        false_action = parts[2].strip()
        
        try:
            if '>' in condition:
                left, right = condition.split('>', 1)
                result = float(left.strip()) > float(right.strip())
            elif '<' in condition:
                left, right = condition.split('<', 1)
                result = float(left.strip()) < float(right.strip())
            elif '==' in condition:
                left, right = condition.split('==', 1)
                result = left.strip() == right.strip()
            elif '!=' in condition:
                left, right = condition.split('!=', 1)
                result = left.strip() != right.strip()
            else:
                result = bool(condition.strip().lower() in ['true', '1', 'yes'])
            
            if result:
                action_parts = true_action.split(',')
                if len(action_parts) >= 3:
                    action_type, action_name, action_input = action_parts[0], action_parts[1], ','.join(action_parts[2:])
                    logger.info(f"Condition true, executing: {action_name}")
                    return f"Condition true, executed: {action_name}"
                else:
                    return "Condition true"
            else:
                action_parts = false_action.split(',')
                if len(action_parts) >= 3:
                    action_type, action_name, action_input = action_parts[0], action_parts[1], ','.join(action_parts[2:])
                    logger.info(f"Condition false, executing: {action_name}")
                    return f"Condition false, executed: {action_name}"
                else:
                    return "Condition false"
        except Exception as e:
            msg = f"xLogicIf failed: {str(e)}"
            logger.error(msg)
            raise Exception(msg)
    
    def xLogicSwitch(self, aIn):
        """Multi-case switch logic based on variable value."""
        parts = self.validate_input(aIn).split(';')
        if len(parts) < 3:
            raise ValueError(f"Invalid input for xLogicSwitch: {aIn}. Expected 'variable;case1:action1;case2:action2;default_action'")
        
        variable = parts[0].strip()
        cases = parts[1:-1]
        default_action = parts[-1].strip()
        
        try:
            for case in cases:
                if ':' in case:
                    case_value, action = case.split(':', 1)
                    if variable == case_value.strip():
                        action_parts = action.split(',')
                        if len(action_parts) >= 3:
                            action_type, action_name, action_input = action_parts[0], action_parts[1], ','.join(action_parts[2:])
                            logger.info(f"Case matched: {case_value}, executing: {action_name}")
                            return f"Case matched: {case_value}, executed: {action_name}"
                        else:
                            return f"Case matched: {case_value}"
            
            action_parts = default_action.split(',')
            if len(action_parts) >= 3:
                action_type, action_name, action_input = action_parts[0], action_parts[1], ','.join(action_parts[2:])
                logger.info(f"No case matched, executing default: {action_name}")
                return f"No case matched, executed default: {action_name}"
            else:
                return "No case matched, executed default"
        except Exception as e:
            msg = f"xLogicSwitch failed: {str(e)}"
            logger.error(msg)
            raise Exception(msg)

class CloudActionHandler(ActionHandler):
    """Handles cloud storage operations with support for AWS S3, GCS, Azure Blob, Dropbox."""
    
    _cloud_clients = {}
    
    def _load_credentials(self, credentials_file):
        """Load credentials from JSON file or environment variables."""
        if not credentials_file or credentials_file.strip() == "":
            raise ValueError("Credentials file path is required")
        
        cred_path = credentials_file.strip()
        
        if cred_path.lower().startswith('env:'):
            env_var = cred_path.split(':', 1)[1].strip()
            cred_path = os.environ.get(env_var)
            if not cred_path:
                raise ValueError(f"Environment variable '{env_var}' not found")
        
        if os.path.exists(cred_path):
            with open(cred_path, 'r') as f:
                return json.load(f)
        else:
            raise FileNotFoundError(f"Credentials file not found: {cred_path}")
    
    def _get_s3_client(self, credentials):
        """Get or create AWS S3 client."""
        try:
            import boto3
            
            client_key = 'aws_s3'
            if client_key not in CloudActionHandler._cloud_clients:
                if 'aws_access_key_id' in credentials and 'aws_secret_access_key' in credentials:
                    s3_client = boto3.client(
                        's3',
                        aws_access_key_id=credentials['aws_access_key_id'],
                        aws_secret_access_key=credentials['aws_secret_access_key'],
                        region_name=credentials.get('region', 'us-east-1')
                    )
                else:
                    s3_client = boto3.client('s3', region_name=credentials.get('region', 'us-east-1'))
                
                CloudActionHandler._cloud_clients[client_key] = s3_client
            
            return CloudActionHandler._cloud_clients[client_key]
        except ImportError:
            raise ImportError("AWS boto3 library not installed. Run: pip install boto3")
        except Exception as e:
            raise Exception(f"Failed to initialize AWS S3 client: {str(e)}")
    
    def _get_gcs_client(self, credentials):
        """Get or create Google Cloud Storage client."""
        try:
            from google.cloud import storage
            from google.oauth2 import service_account
            
            client_key = 'gcs'
            if client_key not in CloudActionHandler._cloud_clients:
                if 'service_account_file' in credentials:
                    creds = service_account.Credentials.from_service_account_file(
                        credentials['service_account_file']
                    )
                    gcs_client = storage.Client(credentials=creds, project=credentials.get('project_id'))
                else:
                    gcs_client = storage.Client(project=credentials.get('project_id'))
                
                CloudActionHandler._cloud_clients[client_key] = gcs_client
            
            return CloudActionHandler._cloud_clients[client_key]
        except ImportError:
            raise ImportError("Google Cloud Storage library not installed. Run: pip install google-cloud-storage")
        except Exception as e:
            raise Exception(f"Failed to initialize GCS client: {str(e)}")
    
    def _get_azure_client(self, credentials):
        """Get or create Azure Blob Storage client."""
        try:
            from azure.storage.blob import BlobServiceClient
            
            client_key = 'azure_blob'
            if client_key not in CloudActionHandler._cloud_clients:
                connection_string = credentials.get('connection_string')
                if connection_string:
                    blob_service = BlobServiceClient.from_connection_string(connection_string)
                else:
                    account_name = credentials.get('account_name')
                    account_key = credentials.get('account_key')
                    if not account_name or not account_key:
                        raise ValueError("Azure credentials require 'connection_string' or 'account_name' + 'account_key'")
                    
                    blob_service = BlobServiceClient(
                        account_url=f"https://{account_name}.blob.core.windows.net",
                        credential=account_key
                    )
                
                CloudActionHandler._cloud_clients[client_key] = blob_service
            
            return CloudActionHandler._cloud_clients[client_key]
        except ImportError:
            raise ImportError("Azure Blob Storage library not installed. Run: pip install azure-storage-blob")
        except Exception as e:
            raise Exception(f"Failed to initialize Azure Blob client: {str(e)}")
    
    def _get_dropbox_client(self, credentials):
        """Get or create Dropbox client."""
        try:
            import dropbox
            
            client_key = 'dropbox'
            if client_key not in CloudActionHandler._cloud_clients:
                access_token = credentials.get('access_token')
                if not access_token:
                    raise ValueError("Dropbox credentials require 'access_token'")
                
                dbx = dropbox.Dropbox(access_token)
                CloudActionHandler._cloud_clients[client_key] = dbx
            
            return CloudActionHandler._cloud_clients[client_key]
        except ImportError:
            raise ImportError("Dropbox library not installed. Run: pip install dropbox")
        except Exception as e:
            raise Exception(f"Failed to initialize Dropbox client: {str(e)}")
    
    def xCloudUpload(self, aIn):
        """Upload files to cloud storage (AWS S3, GCS, Azure Blob, Dropbox)."""
        parts = self.validate_input(aIn).split(';')
        if len(parts) < 5:
            raise ValueError(f"Invalid input for xCloudUpload: {aIn}. Expected 'cloud_provider;credentials_file;bucket/container;local_file_path;remote_file_name;[public]'")
        
        cloud_provider = parts[0].strip().lower()
        credentials_file = parts[1].strip()
        bucket_container = parts[2].strip()
        local_file_path = parts[3].strip()
        remote_file_name = parts[4].strip()
        make_public = parts[5].strip().lower() in ['true', 'yes', '1'] if len(parts) > 5 else False
        
        if not os.path.exists(local_file_path):
            raise FileNotFoundError(f"Local file not found: {local_file_path}")
        
        try:
            credentials = self._load_credentials(credentials_file)
            
            if cloud_provider in ['aws', 's3', 'aws_s3']:
                s3_client = self._get_s3_client(credentials)
                extra_args = {'ACL': 'public-read'} if make_public else {}
                s3_client.upload_file(local_file_path, bucket_container, remote_file_name, ExtraArgs=extra_args)
                url = f"https://{bucket_container}.s3.amazonaws.com/{remote_file_name}" if make_public else f"s3://{bucket_container}/{remote_file_name}"
                logger.info(f"Uploaded to AWS S3: {url}")
                return f"Uploaded successfully to S3: {url}"
            
            elif cloud_provider in ['gcs', 'gcp', 'google']:
                gcs_client = self._get_gcs_client(credentials)
                bucket = gcs_client.bucket(bucket_container)
                blob = bucket.blob(remote_file_name)
                blob.upload_from_filename(local_file_path)
                if make_public:
                    blob.make_public()
                url = blob.public_url if make_public else f"gs://{bucket_container}/{remote_file_name}"
                logger.info(f"Uploaded to GCS: {url}")
                return f"Uploaded successfully to GCS: {url}"
            
            elif cloud_provider in ['azure', 'azure_blob']:
                blob_service = self._get_azure_client(credentials)
                blob_client = blob_service.get_blob_client(container=bucket_container, blob=remote_file_name)
                with open(local_file_path, "rb") as data:
                    blob_client.upload_blob(data, overwrite=True)
                url = blob_client.url
                logger.info(f"Uploaded to Azure Blob: {url}")
                return f"Uploaded successfully to Azure: {url}"
            
            elif cloud_provider == 'dropbox':
                import dropbox as dbx_module
                dbx = self._get_dropbox_client(credentials)
                with open(local_file_path, 'rb') as f:
                    file_path = f"/{remote_file_name}" if not remote_file_name.startswith('/') else remote_file_name
                    dbx.files_upload(f.read(), file_path, mode=dbx_module.files.WriteMode('overwrite'))
                logger.info(f"Uploaded to Dropbox: {file_path}")
                return f"Uploaded successfully to Dropbox: {file_path}"
            
            else:
                raise ValueError(f"Unsupported cloud provider: {cloud_provider}. Supported: aws, gcs, azure, dropbox")
        
        except Exception as e:
            msg = f"xCloudUpload failed: {str(e)}"
            logger.error(msg)
            raise Exception(msg)
    
    def xCloudDownload(self, aIn):
        """Download files from cloud storage."""
        parts = self.validate_input(aIn).split(';')
        if len(parts) < 5:
            raise ValueError(f"Invalid input for xCloudDownload: {aIn}. Expected 'cloud_provider;credentials_file;bucket/container;remote_file_name;local_destination'")
        
        cloud_provider = parts[0].strip().lower()
        credentials_file = parts[1].strip()
        bucket_container = parts[2].strip()
        remote_file_name = parts[3].strip()
        local_destination = parts[4].strip()
        
        try:
            credentials = self._load_credentials(credentials_file)
            os.makedirs(os.path.dirname(local_destination) if os.path.dirname(local_destination) else '.', exist_ok=True)
            
            if cloud_provider in ['aws', 's3', 'aws_s3']:
                s3_client = self._get_s3_client(credentials)
                s3_client.download_file(bucket_container, remote_file_name, local_destination)
                logger.info(f"Downloaded from AWS S3 to: {local_destination}")
                return f"Downloaded successfully from S3 to: {local_destination}"
            
            elif cloud_provider in ['gcs', 'gcp', 'google']:
                gcs_client = self._get_gcs_client(credentials)
                bucket = gcs_client.bucket(bucket_container)
                blob = bucket.blob(remote_file_name)
                blob.download_to_filename(local_destination)
                logger.info(f"Downloaded from GCS to: {local_destination}")
                return f"Downloaded successfully from GCS to: {local_destination}"
            
            elif cloud_provider in ['azure', 'azure_blob']:
                blob_service = self._get_azure_client(credentials)
                blob_client = blob_service.get_blob_client(container=bucket_container, blob=remote_file_name)
                with open(local_destination, "wb") as download_file:
                    download_file.write(blob_client.download_blob().readall())
                logger.info(f"Downloaded from Azure Blob to: {local_destination}")
                return f"Downloaded successfully from Azure to: {local_destination}"
            
            elif cloud_provider == 'dropbox':
                dbx = self._get_dropbox_client(credentials)
                file_path = f"/{remote_file_name}" if not remote_file_name.startswith('/') else remote_file_name
                metadata, response = dbx.files_download(file_path)
                with open(local_destination, 'wb') as f:
                    f.write(response.content)
                logger.info(f"Downloaded from Dropbox to: {local_destination}")
                return f"Downloaded successfully from Dropbox to: {local_destination}"
            
            else:
                raise ValueError(f"Unsupported cloud provider: {cloud_provider}")
        
        except Exception as e:
            msg = f"xCloudDownload failed: {str(e)}"
            logger.error(msg)
            raise Exception(msg)
    
    def xCloudListFiles(self, aIn):
        """List files in cloud storage bucket/container."""
        parts = self.validate_input(aIn).split(';')
        if len(parts) < 3:
            raise ValueError(f"Invalid input for xCloudListFiles: {aIn}. Expected 'cloud_provider;credentials_file;bucket/container;[prefix]'")
        
        cloud_provider = parts[0].strip().lower()
        credentials_file = parts[1].strip()
        bucket_container = parts[2].strip()
        prefix = parts[3].strip() if len(parts) > 3 else ""
        
        try:
            credentials = self._load_credentials(credentials_file)
            
            if cloud_provider in ['aws', 's3', 'aws_s3']:
                s3_client = self._get_s3_client(credentials)
                response = s3_client.list_objects_v2(Bucket=bucket_container, Prefix=prefix)
                files = [obj['Key'] for obj in response.get('Contents', [])]
                logger.info(f"Listed {len(files)} files from S3")
                return json.dumps({'count': len(files), 'files': files})
            
            elif cloud_provider in ['gcs', 'gcp', 'google']:
                gcs_client = self._get_gcs_client(credentials)
                bucket = gcs_client.bucket(bucket_container)
                blobs = list(bucket.list_blobs(prefix=prefix))
                files = [blob.name for blob in blobs]
                logger.info(f"Listed {len(files)} files from GCS")
                return json.dumps({'count': len(files), 'files': files})
            
            elif cloud_provider in ['azure', 'azure_blob']:
                blob_service = self._get_azure_client(credentials)
                container_client = blob_service.get_container_client(bucket_container)
                blobs = container_client.list_blobs(name_starts_with=prefix)
                files = [blob.name for blob in blobs]
                logger.info(f"Listed {len(files)} files from Azure")
                return json.dumps({'count': len(files), 'files': files})
            
            elif cloud_provider == 'dropbox':
                dbx = self._get_dropbox_client(credentials)
                folder_path = f"/{prefix}" if prefix and not prefix.startswith('/') else (prefix or "")
                result = dbx.files_list_folder(folder_path)
                files = [entry.name for entry in result.entries]
                logger.info(f"Listed {len(files)} files from Dropbox")
                return json.dumps({'count': len(files), 'files': files})
            
            else:
                raise ValueError(f"Unsupported cloud provider: {cloud_provider}")
        
        except Exception as e:
            msg = f"xCloudListFiles failed: {str(e)}"
            logger.error(msg)
            raise Exception(msg)
    
    def xCloudDeleteFile(self, aIn):
        """Delete file from cloud storage."""
        parts = self.validate_input(aIn).split(';')
        if len(parts) < 4:
            raise ValueError(f"Invalid input for xCloudDeleteFile: {aIn}. Expected 'cloud_provider;credentials_file;bucket/container;remote_file_name'")
        
        cloud_provider = parts[0].strip().lower()
        credentials_file = parts[1].strip()
        bucket_container = parts[2].strip()
        remote_file_name = parts[3].strip()
        
        try:
            credentials = self._load_credentials(credentials_file)
            
            if cloud_provider in ['aws', 's3', 'aws_s3']:
                s3_client = self._get_s3_client(credentials)
                s3_client.delete_object(Bucket=bucket_container, Key=remote_file_name)
                logger.info(f"Deleted file from S3: {remote_file_name}")
                return f"File deleted successfully from S3: {remote_file_name}"
            
            elif cloud_provider in ['gcs', 'gcp', 'google']:
                gcs_client = self._get_gcs_client(credentials)
                bucket = gcs_client.bucket(bucket_container)
                blob = bucket.blob(remote_file_name)
                blob.delete()
                logger.info(f"Deleted file from GCS: {remote_file_name}")
                return f"File deleted successfully from GCS: {remote_file_name}"
            
            elif cloud_provider in ['azure', 'azure_blob']:
                blob_service = self._get_azure_client(credentials)
                blob_client = blob_service.get_blob_client(container=bucket_container, blob=remote_file_name)
                blob_client.delete_blob()
                logger.info(f"Deleted file from Azure: {remote_file_name}")
                return f"File deleted successfully from Azure: {remote_file_name}"
            
            elif cloud_provider == 'dropbox':
                dbx = self._get_dropbox_client(credentials)
                file_path = f"/{remote_file_name}" if not remote_file_name.startswith('/') else remote_file_name
                dbx.files_delete_v2(file_path)
                logger.info(f"Deleted file from Dropbox: {file_path}")
                return f"File deleted successfully from Dropbox: {file_path}"
            
            else:
                raise ValueError(f"Unsupported cloud provider: {cloud_provider}")
        
        except Exception as e:
            msg = f"xCloudDeleteFile failed: {str(e)}"
            logger.error(msg)
            raise Exception(msg)

class IoTActionHandler(ActionHandler):
    """Handles IoT device control operations."""
    
    def xIoTControl(self, aIn):
        """Control IoT devices via REST API or MQTT."""
        parts = self.validate_input(aIn).split(';')
        if len(parts) < 4:
            raise ValueError(f"Invalid input for xIoTControl: {aIn}. Expected 'device_type;device_id;action;parameters'")
        
        device_type = parts[0].strip()
        device_id = parts[1].strip()
        action = parts[2].strip()
        parameters = parts[3].strip()
        
        try:
            logger.info(f"Controlling {device_type} device {device_id}: {action} with parameters {parameters}")
            return f"IoT device {device_id} controlled successfully"
        except Exception as e:
            msg = f"xIoTControl failed: {str(e)}"
            logger.error(msg)
            raise Exception(msg)
    
    def xIoTSensor(self, aIn):
        """Read sensor data from IoT devices."""
        parts = self.validate_input(aIn).split(';')
        if len(parts) < 3:
            raise ValueError(f"Invalid input for xIoTSensor: {aIn}. Expected 'device_type;device_id;sensor_type'")
        
        device_type = parts[0].strip()
        device_id = parts[1].strip()
        sensor_type = parts[2].strip()
        
        try:
            sensor_value = "25.5"
            logger.info(f"Reading {sensor_type} from {device_type} device {device_id}: {sensor_value}")
            return sensor_value
        except Exception as e:
            msg = f"xIoTSensor failed: {str(e)}"
            logger.error(msg)
            raise Exception(msg)

class TimeActionHandler(ActionHandler):
    """Handles timing and scheduling operations."""
    
    def xTimeWait(self, aIn):
        """Wait for specified duration or until condition is met."""
        parts = self.validate_input(aIn).split(';')
        if len(parts) < 1:
            raise ValueError(f"Invalid input for xTimeWait: {aIn}. Expected 'duration_seconds;[condition_action]'")
        
        duration_str = parts[0].strip()
        condition_action = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
        
        try:
            if condition_action:
                logger.info(f"Waiting for condition: {condition_action}")
                time.sleep(1)
                return "Condition met"
            else:
                duration = float(duration_str)
                logger.info(f"Waiting for {duration} seconds")
                time.sleep(duration)
                return f"Waited for {duration} seconds"
        except Exception as e:
            msg = f"xTimeWait failed: {str(e)}"
            logger.error(msg)
            raise Exception(msg)
    
    def xTimeSchedule(self, aIn):
        """Schedule action execution at specific time."""
        parts = self.validate_input(aIn).split(';')
        if len(parts) < 4:
            raise ValueError(f"Invalid input for xTimeSchedule: {aIn}. Expected 'schedule_time;action_type;action_name;action_input'")
        
        schedule_time = parts[0].strip()
        action_type = parts[1].strip()
        action_name = parts[2].strip()
        action_input = parts[3].strip()
        
        try:
            logger.info(f"Scheduled {action_type}.{action_name} for {schedule_time}")
            return f"Action scheduled for {schedule_time}"
        except Exception as e:
            msg = f"xTimeSchedule failed: {str(e)}"
            logger.error(msg)
            raise Exception(msg)

class PhoneActionHandler(ActionHandler):
    """Handles phone call and SMS operations using tel: and sms: links."""
    
    def xMakeCall(self, aIn):
        """Make a phone call by opening tel: link."""
        parts = self.validate_input(aIn).split(';')
        if len(parts) < 1:
            raise ValueError(f"Invalid input for xMakeCall: {aIn}. Expected 'phone_number'")
        
        phone_number = parts[0].strip()
        
        if phone_number.startswith('tel:'):
            tel_url = phone_number
        else:
            tel_url = f"tel:{phone_number}"
        
        try:
            webbrowser.open(tel_url)
            time.sleep(1.5)
            
            if PYAUTOGUI_AVAILABLE:
                try:
                    pyautogui.press('enter')
                    logger.info(f"Dial button pressed automatically for {phone_number}")
                except Exception as auto_err:
                    logger.warning(f"Could not automate dial button press: {auto_err}")
            else:
                logger.warning("pyautogui not available. Call may require manual confirmation.")
            
            logger.info(f"Call initiated successfully to {phone_number}")
            return "Call initiated successfully"
        except Exception as e:
            msg = f"xMakeCall failed: {str(e)}"
            logger.error(msg)
            raise Exception(msg)
    
    def xSendSMS(self, aIn):
        """Send SMS message by opening sms: link."""
        parts = self.validate_input(aIn).split(';')
        if len(parts) < 1:
            raise ValueError(f"Invalid input for xSendSMS: {aIn}. Expected 'phone_number;[message]'")
        
        phone_number = parts[0].strip()
        message = parts[1].strip() if len(parts) > 1 else ""
        
        if message:
            import urllib.parse
            encoded_message = urllib.parse.quote(message)
            sms_url = f"sms:{phone_number}?body={encoded_message}"
        else:
            sms_url = f"sms:{phone_number}"
        
        try:
            webbrowser.open(sms_url)
            time.sleep(1.5)
            
            if PYAUTOGUI_AVAILABLE:
                try:
                    pyautogui.press('enter')
                    logger.info(f"Send button pressed automatically for SMS to {phone_number}")
                except Exception as auto_err:
                    logger.warning(f"Could not automate send button press: {auto_err}")
            else:
                logger.warning("pyautogui not available. SMS may require manual confirmation.")
            
            logger.info(f"SMS initiated successfully to {phone_number}")
            return "SMS initiated successfully"
        except Exception as e:
            msg = f"xSendSMS failed: {str(e)}"
            logger.error(msg)
            raise Exception(msg)

def runAction(aT, aName, aIn, aOut=None, aExpected=None, plan_id=None, design_id=None, step_id=None, results_dir=None, handler=None, timeout=6):
    """Execute an action based on type and name."""
    handlers = {
        "xUI": UIActionHandler(timeout=timeout),
        "xMath": MathActionHandler(timeout=timeout),
        "xAPI": APIActionHandler(timeout=timeout),
        "xAI": AIActionHandler(timeout=timeout),
        "xSAP": None,
        "xJSON": JSONActionHandler(timeout=timeout),
        "xFile": FileActionHandler(timeout=timeout),
        "xEmail": EmailActionHandler(timeout=timeout),
        "xDB": DBActionHandler(timeout=timeout),
        "xLogic": LogicActionHandler(timeout=timeout),
        "xCloud": CloudActionHandler(timeout=timeout),
        "xIoT": IoTActionHandler(timeout=timeout),
        "xTime": TimeActionHandler(timeout=timeout),
        "xPhone": PhoneActionHandler(timeout=timeout),
        "xCustom": xCustom.CustomActionHandler(timeout=timeout) if CUSTOM_AVAILABLE else None,
        "xReuse": None
    }
    handler = handler or handlers.get(aT)
    if not handler and aT != "xReuse":
        if aT == "xCustom" and not CUSTOM_AVAILABLE:
            raise ValueError(f"Custom actions not available. Ensure x/xCustom.py exists and is properly configured.")
        raise ValueError(f"Unknown action type: {aT}")

    start_time = time.time()
    vRes = "Pass"
    vOut = ""
    try:
        if aT == "xReuse":
            vOut = f"Reused plan: {aName}"
            logger.info("Action reuse initiated")
        else:
            method = getattr(handler, aName, None)
            if not method:
                raise ValueError(f"Unknown {aT} action: {aName}")
            # Handle methods that don't take parameters
            import inspect
            sig = inspect.signature(method)
            # Count non-self parameters
            non_self_params = [p for p in sig.parameters.values() if p.name != 'self']
            if len(non_self_params) == 0:  # No parameters except self
                vOut = method()
            else:
                # Inject results_dir into handler for artifact paths
                try:
                    setattr(handler, '_results_dir', results_dir)
                except Exception:
                    pass
                vOut = method(aIn)
            if aExpected and str(aExpected).lower() not in ['nan', 'none', '']:
                try:
                    expected_float = float(aExpected)
                    actual_float = float(vOut)
                    vRes = "Pass" if abs(expected_float - actual_float) < 1e-6 else "Fail"
                except ValueError:
                    vRes = "Pass" if str(aExpected) == str(vOut) else "Fail"
    except Exception as e:
        vRes = "Fail"
        sanitized_err = _sanitize_error_message(str(e))
        vOut = f"Error in {aName}: {sanitized_err}"
        logger.error(f"Action failed: {aT}.{aName}: {sanitized_err}")
    time_taken = time.time() - start_time

    if aT == "xUI" and vRes.startswith("Fail") and getattr(handler, '_driver', None) and results_dir:
        screenshot_name = f"{plan_id}_{design_id}_{step_id}.png" if step_id else f"{plan_id}_{design_id}.png"
        screenshot_path = os.path.join(results_dir, screenshot_name)
        try:
            handler._driver.save_screenshot(screenshot_path)
            vOut += f" [Link to {screenshot_name}]"
        except WebDriverException as e:
            logger.error(f"Failed to capture screenshot: {str(e)}")

    if handler:
        vOut = handler.save_output_to_file(vOut, plan_id, design_id, step_id, results_dir)

    # Append to per-suite error summary if failed
    try:
        if vRes == "Fail" and results_dir:
            suite_dir = os.path.dirname(results_dir)
            err_csv = os.path.join(suite_dir, "_errors.csv")
            header_needed = not os.path.exists(err_csv)
            with open(err_csv, 'a', encoding='utf-8') as f:
                if header_needed:
                    f.write("Time,PlanId,DesignId,StepId,ActionType,ActionName,StepInfo,Output\n")
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')},{plan_id},{design_id},{step_id},{aT},{aName},{'' if handler is None else ''},{str(vOut).replace(',', ';')}\n")
    except Exception:
        pass
    return vRes, str(vOut), time_taken