import time
from datetime import datetime
import os
import json
import requests
import logging
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
# import win32com.client  # Removed SAP dependency for IoT-only project

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
    """Return (By, value) auto-detecting CSS vs XPath. Supports 'css=' and 'xpath=' prefixes."""
    if locator is None:
        raise ValueError("Locator cannot be None")
    raw = locator.strip()
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

    def find_element(self, locator, clickable=False, retries=2):
        """Find element by auto-detected selector (XPath/CSS) with retries.
        Users: You can prefix with 'css=' or 'xpath=' to be explicit. Otherwise
        the handler guesses based on the first character.
        """
        if not self._driver:
            raise WebDriverException("WebDriver not initialized")
        if not locator or locator.strip() == "":
            raise ValueError("Locator cannot be empty")
        # support explicit type==value pattern still
        if "==" in locator and locator.split("==", 1)[0].lower() in {"id", "name", "class", "xpath", "css"}:
            locator_type, locator_value = locator.split("==", 1)
            by_type = {
                "id": By.ID, "name": By.NAME, "class": By.CLASS_NAME,
                "xpath": By.XPATH, "css": By.CSS_SELECTOR
            }.get(locator_type.lower(), By.XPATH)
        else:
            by_type, locator_value = _detect_selector(locator)
        condition = EC.element_to_be_clickable if clickable else EC.presence_of_element_located
        last_exception = None
        for attempt in range(retries + 1):
            try:
                return WebDriverWait(self._driver, self._timeout).until(condition((by_type, locator_value)))
            except (TimeoutException, NoSuchElementException) as e:
                last_exception = e
                if attempt < retries:
                    time.sleep(1)
                    continue
                raise last_exception

    def xOpenBrowser(self, browser_type="chrome", driver_path=None):
        """Open a web browser using system PATH or a provided driver_path.
        Users: Ensure the browser driver is installed and on PATH.
        """
        if UIActionHandler._shared_driver:
            return "Browser already open"
        browser_type = self.validate_input(browser_type).lower()
        try:
            if browser_type == "chrome":
                # Configure Chrome to minimize noisy logs
                if ChromeOptions:
                    opts = ChromeOptions()
                    opts.add_argument("--log-level=3")
                    opts.add_argument("--disable-logging")
                    opts.add_argument("--disable-gpu")
                    opts.add_argument("--no-sandbox")
                    opts.add_argument("--disable-dev-shm-usage")
                else:
                    opts = None

                if ChromeService:
                    service = ChromeService(executable_path=driver_path) if driver_path else ChromeService()
                    try:
                        # Suppress chromedriver logs if supported
                        service.log_path = os.devnull  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    self._driver = webdriver.Chrome(service=service, options=opts)
                else:
                    # Fallback older API
                    if driver_path:
                        self._driver = webdriver.Chrome(executable_path=driver_path)
                    else:
                        self._driver = webdriver.Chrome()
            elif browser_type == "firefox":
                if driver_path:
                    self._driver = webdriver.Firefox(executable_path=driver_path)
                else:
                    self._driver = webdriver.Firefox()
            elif browser_type == "edge":
                if driver_path:
                    self._driver = webdriver.Edge(executable_path=driver_path)
                else:
                    self._driver = webdriver.Edge()
            else:
                raise ValueError(f"Unsupported browser: {browser_type}")
            self._driver.maximize_window()
            UIActionHandler._shared_driver = self._driver
            logger.info(f"Opened {browser_type} browser")
            return f"Opened {browser_type} browser"
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
        """
        if not self._driver:
            raise WebDriverException("Driver not initialized")
        try:
            element = self.find_element(locator, clickable=True)
            element.click()
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
        """
        if not self._driver:
            raise WebDriverException("Driver not initialized")
        try:
            self.find_element(locator)
            logger.info("Element wait successful")
            return "Element is present"
        except Exception as e:
            msg = f"xWaitFor failed for locator '{locator}': {str(e)}"
            logger.error(msg)
            raise Exception(_save_error_artifacts(self._driver, getattr(self, '_results_dir', None), None, None, None, msg))

    def xCloseBrowser(self):
        """Close the browser."""
        if UIActionHandler._shared_driver:
            try:
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
        value = self._get_json_value(APIActionHandler._last_response, key_path)
        result = str(value == expected_value)
        logger.info(f"JSON comparison result: {result}")
        return result

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

def runAction(aT, aName, aIn, aOut=None, aExpected=None, plan_id=None, design_id=None, step_id=None, results_dir=None, handler=None, timeout=6):
    """Execute an action based on type and name."""
    handlers = {
        "xUI": UIActionHandler(timeout=timeout),
        "xMath": MathActionHandler(timeout=timeout),
        "xAPI": APIActionHandler(timeout=timeout),
        "xAI": AIActionHandler(timeout=timeout),
        "xSAP": None, # Removed SAPActionHandler
        "xJSON": JSONActionHandler(timeout=timeout)
    }
    handler = handler or handlers.get(aT)
    if not handler:
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