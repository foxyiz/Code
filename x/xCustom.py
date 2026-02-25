"""
Custom Actions Handler - For End Users

This file allows end users to add their own custom functions that can be used
in test plans. Simply add methods to the CustomActionHandler class below.

Usage in y2Actions.csv:
- ActionType: xCustom
- ActionName: your_method_name (without the 'x' prefix)
- Input: your input parameters (semicolon-separated if multiple)

Example:
    ActionType: xCustom
    ActionName: xMyCustomFunction
    Input: param1;param2;param3

The method should be named with 'x' prefix in this file:
    def xMyCustomFunction(self, aIn):
        # Your code here
        return "result"
"""

import logging
import os
import sys
import json
import requests
from urllib.parse import urlencode

# Import the base ActionHandler class
try:
    from x.xActions import ActionHandler
except ImportError:
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
    from xActions import ActionHandler  # type: ignore[import-not-found]

logger = logging.getLogger(__name__)


class CustomActionHandler(ActionHandler):
    """
    Custom Actions Handler - Add your own functions here.
    
    All methods should:
    1. Start with 'x' prefix (e.g., xMyFunction)
    2. Accept 'aIn' as the first parameter (input string, semicolon-separated)
    3. Return a string result
    4. Use self.validate_input(aIn) to get and validate input
    
    Example:
        def xMyCustomFunction(self, aIn):
            parts = self.validate_input(aIn).split(';')
            # Your logic here
            return "Success"
    """
    
    def __init__(self, timeout=6):
        super().__init__(timeout)
    
    # ============================================
    # ADD YOUR CUSTOM FUNCTIONS BELOW THIS LINE
    # ============================================
    
    def xExampleFunction(self, aIn):
        """
        Example custom function.
        
        Input format: "param1;param2"
        Returns: A string result
        """
        parts = self.validate_input(aIn).split(';')
        if len(parts) < 2:
            raise ValueError(f"Invalid input for xExampleFunction: {aIn}. Expected 'param1;param2'")
        
        param1 = parts[0].strip()
        param2 = parts[1].strip()
        
        logger.info(f"Example function called with: {param1}, {param2}")
        result = f"Processed: {param1} and {param2}"
        return result
    
    def xOAuthFormRequest(self, aIn):
        """
        Perform an OAuth2 token request using application/x-www-form-urlencoded body.

        Input format (semicolon-separated):
            token_url;client_id;client_secret;grant_type;scope;extra_params

        - token_url (required): Token endpoint URL
        - client_id (required)
        - client_secret (required)
        - grant_type (required): e.g. client_credentials, password, refresh_token
        - scope (optional): space-separated scopes
        - extra_params (optional): JSON string of additional form parameters, or key1=value1&key2=value2

        Returns:
            JSON string of the token endpoint response or raises Exception on failure.
        """
        raw = self.validate_input(aIn)
        parts = [p.strip() for p in raw.split(';')]
        if len(parts) < 4:
            raise ValueError(f"Invalid input for xOAuthFormRequest: {aIn}. "
                             f"Expected at least 'token_url;client_id;client_secret;grant_type'")

        token_url = parts[0]
        client_id = parts[1]
        client_secret = parts[2]
        grant_type = parts[3]
        scope = parts[4] if len(parts) > 4 and parts[4] else None
        extra = parts[5] if len(parts) > 5 and parts[5] else None

        # Build form data
        form = {
            "grant_type": grant_type,
            "client_id": client_id,
            "client_secret": client_secret
        }
        if scope:
            form["scope"] = scope

        # Parse extra parameters if provided
        if extra:
            # Try JSON first
            try:
                extra_obj = json.loads(extra)
                if isinstance(extra_obj, dict):
                    form.update(extra_obj)
            except Exception:
                # Fallback: parse as key1=value1&key2=value2
                try:
                    for kv in extra.split('&'):
                        if '=' in kv:
                            k, v = kv.split('=', 1)
                            form[k] = v
                except Exception:
                    # ignore parse errors and continue with basic form
                    pass

        headers = {
            "Content-Type": "application/x-www-form-urlencoded"
        }

        try:
            timeout_val = getattr(self, '_timeout', 6)
            resp = requests.post(token_url, data=form, headers=headers, timeout=timeout_val)
        except Exception as e:
            logger.error(f"OAuth request failed: {str(e)}")
            raise

        # Return JSON string or text on failure
        try:
            resp.raise_for_status()
        except Exception as e:
            # include response body for diagnostics
            body = resp.text if resp is not None else "<no response>"
            raise Exception(f"OAuth token request failed ({resp.status_code}): {body}") from e

        # Try to return JSON string
        try:
            return json.dumps(resp.json())
        except Exception:
            return resp.text
    
    # Add more custom functions below as needed:
    # 
    # def xYourFunction(self, aIn):
    #     """Your function description."""
    #     parts = self.validate_input(aIn).split(';')
    #     # Your implementation here
    #     return "result"