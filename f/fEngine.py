import os
import shutil
import json
import time
import pandas as pd
import argparse
import logging
import sys
import subprocess
import platform
from datetime import datetime
import multiprocessing
import io
import re
import difflib
from concurrent.futures import ProcessPoolExecutor

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

def _detect_project_root():
    """Return runtime project root for dev and frozen executable modes."""
    dev_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    if not getattr(sys, 'frozen', False):
        return dev_root

    exe_dir = os.path.abspath(os.path.dirname(sys.executable))
    parent = os.path.dirname(exe_dir)
    candidates = [exe_dir]
    if parent and parent != exe_dir:
        candidates.append(parent)

    # Prefer a directory that clearly looks like the project root.
    for cand in candidates:
        if os.path.isfile(os.path.join(cand, 'BUILD.md')):
            return cand
        if all(os.path.isdir(os.path.join(cand, d)) for d in ('f', 'x', 'y')):
            return cand

    # Common packaged layout: executable inside <root>/f/Foxyiz.exe
    if os.path.basename(exe_dir).lower() == 'f' and parent and parent != exe_dir:
        return parent

    return exe_dir


PROJECT_ROOT = _detect_project_root()
try:
    from dotenv import load_dotenv, dotenv_values
except ImportError:
    load_dotenv = None
    dotenv_values = None
try:
    import x.xActions as xActions
except ImportError:
    # Fallback for bundled execution: add data folder 'x' to sys.path and import module
    x_dir = None
    try:
        x_dir = os.path.abspath(os.path.join(sys._MEIPASS, 'x'))  # type: ignore[attr-defined]
    except Exception:
        pass
    if not x_dir or not os.path.isdir(x_dir):
        x_dir = os.path.abspath(os.path.join(PROJECT_ROOT, 'x'))
    if x_dir not in sys.path:
        sys.path.insert(0, x_dir)
    import xActions as xActions  # type: ignore[import-not-found]

# Configure logging to suppress technical details
logging.basicConfig(level=logging.ERROR, format='%(message)s')
logger = logging.getLogger(__name__)

# Suppress third-party library logs
logging.getLogger('selenium').setLevel(logging.ERROR)
logging.getLogger('urllib3').setLevel(logging.ERROR)
logging.getLogger('requests').setLevel(logging.ERROR)

# User-friendly output functions
def print_header(title):
    """Print a formatted header for sections."""
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

def print_progress(current, total, item_name="items"):
    """Print progress information."""
    percentage = (current / total) * 100 if total > 0 else 0
    bar_length = 30
    filled_length = int(bar_length * current // total) if total > 0 else 0
    bar = '█' * filled_length + '-' * (bar_length - filled_length)
    progress_text = f"Progress: |{bar}| {percentage:.1f}% ({current}/{total} {item_name})"
    # When stdout is not an interactive terminal (e.g., worker StringIO buffer),
    # printing CR-based progress causes replay artifacts and duplicated-looking logs.
    if hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
        print(f"\r{progress_text}", end='', flush=True)
    else:
        print(progress_text)

def print_status(message, status="INFO"):
    """Print status messages with formatting."""
    status_symbols = {
        "INFO": "ℹ️",
        "SUCCESS": "✅", 
        "WARNING": "⚠️",
        "ERROR": "❌",
        "RUNNING": "🔄"
    }
    symbol = status_symbols.get(status, "•")
    print(f"{symbol} {message}")

def print_summary(stats):
    """Print execution summary."""
    print_header("EXECUTION SUMMARY")
    print(f"📊 Total Plans: {stats['total_plans']}")
    print(f"✅ Passed: {stats['passed']}")
    print(f"❌ Failed: {stats['failed']}")
    print(f"⏱️  Total Time: {stats['total_time']:.2f} seconds")
    print(f"📁 Results saved to: {stats['output_dir']}")
    print(f"🌐 Dashboard: {stats['dashboard_path']}")
    print(f"{'='*60}\n")

def cleanup_empty_directories(directory):
    """Remove empty directories recursively, but keep the root directory."""
    if not os.path.exists(directory):
        return 0
    
    removed_count = 0
    # Walk through all subdirectories, starting from the deepest ones
    for root, dirs, files in os.walk(directory, topdown=False):
        # Skip the root directory itself
        if root == directory:
            continue
        
        # Check if directory is empty (no files and no subdirectories)
        try:
            if not os.listdir(root):
                os.rmdir(root)
                removed_count += 1
        except OSError:
            # Directory might have been removed already or permission issue
            pass
    
    return removed_count

def kill_chromedriver_processes():
    """Kill any running chromedriver processes to prevent conflicts."""
    try:
        system = platform.system()
        
        if system == 'Windows':
            # Windows: use taskkill to kill chromedriver.exe processes
            try:
                # Kill chromedriver.exe processes
                subprocess.run(['taskkill', '/F', '/IM', 'chromedriver.exe'], 
                            stdout=subprocess.DEVNULL, 
                            stderr=subprocess.DEVNULL,
                            timeout=5)
            except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
                pass  # Process might not exist, or taskkill not available
        elif system == 'Linux':
            # Linux: use pkill to kill chromedriver processes
            try:
                subprocess.run(['pkill', '-f', 'chromedriver'], 
                            stdout=subprocess.DEVNULL, 
                            stderr=subprocess.DEVNULL,
                            timeout=5)
            except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
                pass
        elif system == 'Darwin':  # macOS
            # macOS: use pkill to kill chromedriver processes
            try:
                subprocess.run(['pkill', '-f', 'chromedriver'], 
                            stdout=subprocess.DEVNULL, 
                            stderr=subprocess.DEVNULL,
                            timeout=5)
            except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
                pass
    except Exception:
        pass  # Don't fail if process killing fails

# Global cache for action results
action_cache = {}

# Environment variables from .env (sensitive placeholders for y3Designs)
_env_dict = {}

def _env_path():
    """Return path to .env file: next to script/exe, then cwd."""
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = PROJECT_ROOT
    for d in (base, os.getcwd()):
        p = os.path.join(d, '.env')
        if os.path.isfile(p):
            return p
    return None

def load_env():
    """Load .env into os.environ and return dict of key=value for design placeholder substitution."""
    global _env_dict
    path = _env_path()
    if not path:
        _env_dict = {}
        return _env_dict
    if load_dotenv and dotenv_values:
        load_dotenv(path)
        raw = dotenv_values(path)
        _env_dict = {k: (v if v is not None else '') for k, v in (raw or {}).items()}
        _env_dict = _normalize_env_dict_keys(_env_dict)
        return _env_dict
    # Fallback: parse .env manually (KEY=VALUE, strip quotes, skip comments)
    _env_dict = {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    k, _, v = line.partition('=')
                    key = k.strip()
                    val = v.strip()
                    if len(val) >= 2 and (val.startswith('"') and val.endswith('"') or val.startswith("'") and val.endswith("'")):
                        val = val[1:-1]
                    os.environ[key] = val
                    _env_dict[key] = val
        _env_dict = _normalize_env_dict_keys(_env_dict)
    except Exception:
        pass
    return _env_dict

def _normalize_env_dict_keys(env_dict):
    """Add key aliases so y3Designs placeholders match .env despite stray quotes (e.g. KEY\"=)."""
    if not env_dict:
        return env_dict
    extra = {}
    for k, v in env_dict.items():
        k2 = k.strip().strip('"').strip("'")
        if k2 and k2 != k and k2 not in env_dict:
            extra[k2] = v
    if extra:
        out = dict(env_dict)
        out.update(extra)
        return out
    return env_dict

def _substitute_env_in_value(data_value):
    """Replace any .env placeholder keys in data_value with their values."""
    if not data_value or not _env_dict:
        return data_value
    s = str(data_value)
    # Replace longest keys first to avoid partial replacements (e.g. KEY vs KEY2)
    for key in sorted(_env_dict.keys(), key=len, reverse=True):
        val = _env_dict.get(key)
        if val is None:
            val = ''
        s = s.replace(str(key), str(val))
    return s

def _default_main_config_path():
    """Default path to the main JSON config.

    Development: repo layout has ``f/fStart.json`` relative to project root.

    Frozen (PyInstaller): the exe is often placed in ``f/`` next to ``fStart.json``.
    Using ``f/fStart.json`` relative to the exe dir would incorrectly resolve to
    ``f/f/fStart.json``. Prefer ``fStart.json`` beside the executable, or
    ``f/fStart.json`` when the exe lives at project root and config is under ``f/``.
    """
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.abspath(os.path.dirname(sys.executable))
        beside_exe = os.path.join(exe_dir, 'fStart.json')
        under_f = os.path.join(exe_dir, 'f', 'fStart.json')
        if os.path.isfile(beside_exe):
            return 'fStart.json'
        if os.path.isfile(under_f):
            return 'f/fStart.json'
        return 'fStart.json'
    return 'f/fStart.json'


def _frozen_data_search_bases():
    """Directories to search for user data (y/, z/ beside exe, etc.) when running frozen.

    If the exe lives in ``f/Foxyiz.exe``, repo data usually sits one level up (``../y/``),
    not under ``f/y/``. Try exe directory first, then its parent.
    """
    exe_dir = os.path.abspath(os.path.dirname(sys.executable))
    bases = [exe_dir]
    parent = os.path.dirname(exe_dir)
    if parent and parent != exe_dir:
        bases.append(parent)
    return bases


def _results_z_root():
    """Absolute path to the ``z`` folder where run outputs are written.

    Development: ``<project root>/z`` so results do not depend on current working directory.

    Frozen: if ``y/`` sits next to the exe, use ``<exe_dir>/z``; if ``y/`` is only under the
    parent (exe inside ``f/``), use ``<parent>/z`` so results align with the same layout as
    ``python f/fEngine.py`` from the repo.
    """
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.abspath(os.path.dirname(sys.executable))
        parent = os.path.dirname(exe_dir)
        if os.path.isdir(os.path.join(exe_dir, 'y')):
            return os.path.join(exe_dir, 'z')
        if parent and os.path.isdir(os.path.join(parent, 'y')):
            return os.path.join(parent, 'z')
        return os.path.join(exe_dir, 'z')
    return os.path.join(PROJECT_ROOT, 'z')


def _resource_path(relative_path):
    """Get absolute path to resource, works for dev and PyInstaller."""
    # If running as PyInstaller executable
    if getattr(sys, 'frozen', False):
        # First try the bundled resources in _MEIPASS
        try:
            bundled_path = os.path.abspath(os.path.join(sys._MEIPASS, relative_path))  # type: ignore[attr-defined]
            if os.path.exists(bundled_path):
                return bundled_path
        except Exception:
            pass
        # Non-bundled files (y/, optional z/): exe dir, then parent (exe inside f/)
        for base in _frozen_data_search_bases():
            candidate = os.path.abspath(os.path.join(base, relative_path))
            if os.path.exists(candidate):
                return candidate
        return os.path.abspath(os.path.join(_frozen_data_search_bases()[0], relative_path))
    else:
        # Development mode: use script directory
        base_path = PROJECT_ROOT
        return os.path.abspath(os.path.join(base_path, relative_path))


def _resolve_cli_relative_path(slash_path):
    """Resolve a path relative to project data (y/, z/). Same search order as _resource_path for non-bundled files."""
    parts = [p for p in str(slash_path).replace("\\", "/").split("/") if p and p != "."]
    if not parts:
        raise ValueError("Invalid YPAD path")
    if getattr(sys, "frozen", False):
        for base in _frozen_data_search_bases():
            cand = os.path.abspath(os.path.join(base, *parts))
            if os.path.exists(cand):
                return cand
        return os.path.abspath(os.path.join(_frozen_data_search_bases()[0], *parts))
    return os.path.abspath(os.path.join(PROJECT_ROOT, *parts))


def _data_root_containing(abs_path):
    """Project root or frozen exe dir/parent that contains abs_path (for CLI path normalization)."""
    abs_path = os.path.abspath(abs_path)
    if getattr(sys, "frozen", False):
        for base in _frozen_data_search_bases():
            b = os.path.abspath(base)
            try:
                if os.path.commonpath([b, abs_path]) == b:
                    return b
            except ValueError:
                continue
        return _frozen_data_search_bases()[0]
    return PROJECT_ROOT


def load_config(config_path):
    """Load configuration from a JSON file."""
    resolved = config_path
    if not os.path.isabs(config_path):
        # For exe: First check in the exe directory (user-provided configs)
        if getattr(sys, 'frozen', False):
            exe_dir_path = os.path.join(os.path.dirname(sys.executable), config_path)
            if os.path.exists(exe_dir_path):
                resolved = exe_dir_path
            else:
                # Fall back to bundled resources
                resolved = _resource_path(config_path)
        else:
            resolved = _resource_path(config_path)
    with open(resolved, 'r') as f:
        return json.load(f)

def load_csv(file_path):
    """
    Load data file (CSV, Excel, TXT, or JSON) into a DataFrame.
    Supports multiple file formats while maintaining the same template structure.
    """
    resolved = file_path
    if not os.path.isabs(file_path):
        resolved = _resource_path(file_path)
    
    # Get file extension to determine file type
    file_ext = os.path.splitext(resolved)[1].lower()
    
    last_error = None
    df = None
    
    # Read file based on extension
    if file_ext in ['.csv', '.txt']:
        # CSV and TXT files - treat both as CSV (TXT can be tab or comma delimited)
        # Try comma first, then tab delimiter for TXT files
        try:
            df = pd.read_csv(resolved, encoding='utf-8-sig', quotechar='"', doublequote=True)  # utf-8-sig handles BOM
        except Exception as e:
            last_error = e
            # For TXT files, try tab delimiter
            if file_ext == '.txt':
                try:
                    df = pd.read_csv(resolved, encoding='utf-8-sig', sep='\t', quotechar='"', doublequote=True)
                except Exception as e2:
                    last_error = e2
                    # Try comma delimiter
                    try:
                        df = pd.read_csv(resolved, encoding='utf-8-sig', sep=',', quotechar='"', doublequote=True)
                    except Exception as e3:
                        last_error = e3
                        # Fallback: try without quotechar specification
                        try:
                            df = pd.read_csv(resolved, encoding='utf-8-sig', doublequote=True)
                        except Exception as e4:
                            last_error = e4
                            # Last resort: try default encoding
                            try:
                                df = pd.read_csv(resolved, doublequote=True)
                            except Exception as e5:
                                last_error = e5
                                # Final fallback: basic read
                                try:
                                    df = pd.read_csv(resolved)
                                except Exception as e6:
                                    raise Exception(f"Failed to read TXT/CSV file '{resolved}'. Last error: {str(e6)}. "
                                                  f"Previous errors: {str(e)}, {str(e2)}, {str(e3)}, {str(e4)}, {str(e5)}")
            else:
                # For CSV files, try fallback options
                try:
                    df = pd.read_csv(resolved, encoding='utf-8-sig', doublequote=True)
                except Exception as e2:
                    last_error = e2
                    # Last resort: try default encoding
                    try:
                        df = pd.read_csv(resolved, doublequote=True)
                    except Exception as e3:
                        last_error = e3
                        # Final fallback: basic read
                        try:
                            df = pd.read_csv(resolved)
                        except Exception as e4:
                            # If all attempts fail, raise with helpful message
                            raise Exception(f"Failed to read CSV file '{resolved}'. Last error: {str(e4)}. "
                                          f"Previous errors: {str(e)}, {str(e2)}, {str(e3)}")
    
    elif file_ext in ['.xlsx', '.xls']:
        # Excel files
        try:
            df = pd.read_excel(resolved, engine='openpyxl')
        except ImportError:
            raise Exception(f"Excel file support requires 'openpyxl' package. Please install it: pip install openpyxl")
        except Exception as e:
            last_error = e
            # Try with xlrd engine for older .xls files
            if file_ext == '.xls':
                try:
                    df = pd.read_excel(resolved, engine='xlrd')
                except ImportError:
                    raise Exception(f"Excel .xls file support requires 'xlrd' package. Please install it: pip install xlrd")
                except Exception as e2:
                    raise Exception(f"Failed to read Excel file '{resolved}'. Last error: {str(e2)}. "
                                  f"Previous error: {str(e)}")
            else:
                raise Exception(f"Failed to read Excel file '{resolved}'. Error: {str(e)}")
    
    elif file_ext == '.json':
        # JSON files
        try:
            df = pd.read_json(resolved, orient='records')
        except Exception as e:
            last_error = e
            # Try reading as JSON lines (JSONL) format
            try:
                df = pd.read_json(resolved, lines=True)
            except Exception as e2:
                last_error = e2
                # Try reading JSON and converting to DataFrame
                try:
                    with open(resolved, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    if isinstance(data, list):
                        df = pd.DataFrame(data)
                    elif isinstance(data, dict):
                        # If it's a dict, try to find a list or convert values
                        if 'data' in data and isinstance(data['data'], list):
                            df = pd.DataFrame(data['data'])
                        else:
                            df = pd.DataFrame([data])
                    else:
                        raise Exception(f"Unsupported JSON structure in '{resolved}'")
                except Exception as e3:
                    raise Exception(f"Failed to read JSON file '{resolved}'. Last error: {str(e3)}. "
                                  f"Previous errors: {str(e)}, {str(e2)}")
    
    else:
        raise Exception(f"Unsupported file format '{file_ext}' for file '{resolved}'. "
                       f"Supported formats: .csv, .txt, .xlsx, .xls, .json")
    
    if df is None or df.empty:
        raise Exception(f"File '{resolved}' is empty or could not be read properly")
    
    # Clean column names: strip whitespace and remove quotes (same template as CSV)
    df.columns = df.columns.str.strip().str.strip('"').str.strip("'")
    
    # Fix PlanId column to be string if it exists (same template as CSV)
    if 'PlanId' in df.columns:
        # Only add 'P' prefix if it's not already there
        df['PlanId'] = df['PlanId'].astype(str)
        df['PlanId'] = df['PlanId'].apply(lambda x: x if x.startswith('P') else 'P' + x)
    
    return df

def generate_dashboard(df, output_dir, ypad_name):
    """Generate modern interactive HTML dashboard from results DataFrame."""
    # Calculate plan-level results (a plan passes only if ALL its actions pass)
    plan_results = df.groupby('PlanId').agg({
        'Result': lambda x: 'Pass' if (x == 'Pass').all() else ('Pending' if x.isna().all() else 'Fail')
    }).reset_index()
    
    # Calculate summary statistics at plan level
    summary_plans = {
        "Total": len(plan_results),
        "Executed": len(plan_results[plan_results['Result'] != 'Pending']),
        "Pending": len(plan_results[plan_results['Result'] == 'Pending']),
        "Time Taken (s)": round(df['TimeTaken'].sum(), 2),
        "Pass": len(plan_results[plan_results['Result'] == 'Pass']),
        "Fail": len(plan_results[plan_results['Result'] == 'Fail'])
    }
    summary_actions = {
        "Total": len(df),
        "Executed": len(df[df['Result'].notna()]),
        "Pending": len(df[df['Result'].isna()]),
        "Time Taken (s)": round(df['TimeTaken'].sum(), 2),
        "Pass": len(df[df['Result'] == 'Pass']),
        "Fail": len(df[df['Result'] == 'Fail'])
    }

    # Load the template from z/zDash_template.html
    template_path = _resource_path('z/zDash_template.html')
    
    html_content = None
    try:
        with open(template_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
    except FileNotFoundError:
        # Fallback to inline template if external file not found
        html_content = _get_inline_dashboard_template()

    # Prepare data for JavaScript
    results_data = []
    for _, row in df.iterrows():
        # Check for screenshot file (from fEngine.py v1)
        screenshot_file = None
        if row.get('ActionType') == 'xUI' and row.get('Result') == 'Fail':
            # Look for screenshot file in results directory
            potential_screenshot = f"{row['PlanId']}_{row['DesignId']}_{row['StepId']}.png"
            screenshot_path = os.path.join(output_dir, potential_screenshot)
            if os.path.exists(screenshot_path):
                screenshot_file = potential_screenshot
        
        # Also check for Screenshot column (from fEngine2.py v2)
        if not screenshot_file and hasattr(row, 'Screenshot') and pd.notna(row.get('Screenshot')):
            screenshot_file = str(row['Screenshot'])

        # Prepare error details for failed actions
        error_details = None
        if row.get('Result') == 'Fail':
            error_details = {
                'type': 'TestFailure',  # fEngine.py v1 uses 'TestFailure'
                'message': str(row.get('Output', 'Test failed')),
                'url': None,  # Could be extracted from output if available
                'stackTrace': None  # Could be extracted from logs if available
            }

        result_item = {
            'designId': str(row.get('DesignId', '')),
            'planId': str(row.get('PlanId', '')),
            'stepId': str(row.get('StepId', '')),
            'stepInfo': str(row.get('StepInfo', '')),
            'actionType': str(row.get('ActionType', '')),
            'actionName': str(row.get('ActionName', '')),
            'input': str(row.get('Input', '')),
            'output': str(row.get('Output', '')),
            'expected': str(row.get('Expected', '')),
            'result': str(row.get('Result', '')) if pd.notna(row.get('Result')) else None,
            'time': str(row.get('Time', datetime.now().strftime("%H:%M:%S"))),
            'timeTaken': round(float(row.get('TimeTaken', 0)), 2),
            'critical': str(row.get('Critical', 'N')),
            'screenshot': screenshot_file,
            'errorDetails': error_details
        }
        results_data.append(result_item)

    # Convert results to JSON for JavaScript
    results_json = json.dumps(results_data, indent=2)

    # Replace template placeholders (using plan-level stats for summary cards)
    replacements = {
        '{YPAD_NAME}': ypad_name,
        '{GENERATION_TIME}': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        '{TOTAL_ACTIONS}': str(summary_plans["Total"]),  # Show total plans
        '{PASSED_ACTIONS}': str(summary_plans["Pass"]),  # Show passed plans
        '{FAILED_ACTIONS}': str(summary_plans["Fail"]),  # Show failed plans
        '{PENDING_ACTIONS}': str(summary_plans["Pending"]),  # Show pending plans
        '{TOTAL_TIME}': str(summary_plans["Time Taken (s)"])
    }

    # Apply basic replacements
    for placeholder, value in replacements.items():
        html_content = html_content.replace(placeholder, value)
    
    # Replace the mock data section with actual results (from fEngine.py v1 - more robust)
    mock_data_start = html_content.find('// MOCK DATA FOR DEMONSTRATION')
    mock_data_end = html_content.find('// Replace the above mock data')
    
    if mock_data_start != -1 and mock_data_end != -1:
        # Replace the entire mock data section
        replacement_section = f"""// ACTUAL DATA FROM CSV RESULTS
        const testResults = {results_json};

        """
        html_content = (html_content[:mock_data_start] + 
                       replacement_section + 
                       html_content[mock_data_end:])
    else:
        # Fallback: try to replace the testResults array directly (from fEngine.py v1)
        if 'const testResults = [' in html_content:
            # Find and replace the entire testResults array
            start_idx = html_content.find('const testResults = [')
            if start_idx != -1:
                # Find the end of the array (matching closing bracket)
                bracket_count = 0
                end_idx = start_idx + len('const testResults = ')
                for i, char in enumerate(html_content[end_idx:], end_idx):
                    if char == '[':
                        bracket_count += 1
                    elif char == ']':
                        bracket_count -= 1
                        if bracket_count == 0:
                            end_idx = i + 1
                            break
                
                # Replace the array
                html_content = (html_content[:start_idx] + 
                               f'const testResults = {results_json}' +
                               html_content[end_idx:])
        else:
            # Try fEngine2.py v2 approach
            mock_data_start = 'const testResults = ['
            mock_data_end = '];'
            start_idx = html_content.find(mock_data_start)
            if start_idx != -1:
                end_idx = html_content.find(mock_data_end, start_idx) + len(mock_data_end)
                if end_idx != -1:
                    actual_data = f"const testResults = {json.dumps(results_data, indent=8)};"
                    html_content = html_content[:start_idx] + actual_data + html_content[end_idx:]
                else:
                    # Last resort: append the data
                    script_tag = html_content.find('<script>')
                    if script_tag != -1:
                        insert_pos = script_tag + len('<script>')
                        html_content = (html_content[:insert_pos] + 
                                       f'\n        const testResults = {results_json};\n' +
                                       html_content[insert_pos:])

    # Write the dashboard file
    dashboard_path = os.path.join(output_dir, f"{ypad_name}_zDash.html")
    with open(dashboard_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    return dashboard_path

def _get_inline_dashboard_template():
    """Fallback inline dashboard template if external file is not available."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FoXYiZ Test Dashboard - {YPAD_NAME}</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 20px; background: #f8fafc; }
        .container { max-width: 1200px; margin: 0 auto; }
        .header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 2rem; border-radius: 12px; margin-bottom: 2rem; }
        .summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 2rem; }
        .card { background: white; padding: 1.5rem; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .card h3 { margin: 0 0 0.5rem 0; color: #64748b; font-size: 0.875rem; text-transform: uppercase; }
        .card .value { font-size: 2rem; font-weight: bold; color: #1e293b; }
        .results { background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #e2e8f0; }
        th { background: #f8fafc; font-weight: 600; }
        .status-pass { background: #dcfce7; color: #166534; padding: 4px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 600; }
        .status-fail { background: #fecaca; color: #991b1b; padding: 4px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 600; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>FoXYiZ Test Dashboard</h1>
            <p>Test Suite: <strong>{YPAD_NAME}</strong> | Generated: <strong>{GENERATION_TIME}</strong></p>
        </div>
        <div class="summary">
            <div class="card"><h3>Total Tests</h3><div class="value">{TOTAL_ACTIONS}</div></div>
            <div class="card"><h3>Passed</h3><div class="value">{PASSED_ACTIONS}</div></div>
            <div class="card"><h3>Failed</h3><div class="value">{FAILED_ACTIONS}</div></div>
            <div class="card"><h3>Pending</h3><div class="value">{PENDING_ACTIONS}</div></div>
            <div class="card"><h3>Duration</h3><div class="value">{TOTAL_TIME}s</div></div>
        </div>
        <div class="results">
            <table id="results-table">
                <thead>
                    <tr><th>Plan ID</th><th>Step</th><th>Action</th><th>Status</th><th>Duration</th></tr>
                </thead>
                <tbody id="results-tbody"></tbody>
            </table>
        </div>
    </div>
    <script>
        const testResults = [];
        // Fallback simple table rendering
        const tbody = document.getElementById('results-tbody');
        testResults.forEach(result => {
            const row = document.createElement('tr');
            const statusClass = result.result === 'Pass' ? 'status-pass' : 'status-fail';
            row.innerHTML = `
                <td><strong>${result.planId}</strong></td>
                <td>${result.stepId} - ${result.stepInfo}</td>
                <td>${result.actionType} → ${result.actionName}</td>
                <td><span class="${statusClass}">${result.result}</span></td>
                <td>${result.timeTaken ? result.timeTaken.toFixed(2) + 's' : '-'}</td>
            `;
            tbody.appendChild(row);
        });
    </script>
</body>
</html>"""

def _generate_basic_dashboard(df, output_dir, ypad_name):
    """Fallback function to generate basic dashboard if template is not found (from fEngine2.py v2)."""
    summary_plans = {
        "Total": len(df['PlanId'].unique()),
        "Executed": len(df[df['Result'].notna()]['PlanId'].unique()),
        "Pending": len(df[df['Result'].isna()]['PlanId'].unique()),
        "Time Taken (s)": round(df['TimeTaken'].sum(), 2),
        "Pass": len(df[df['Result'] == 'Pass']['PlanId'].unique()),
        "Fail": len(df[df['Result'] == 'Fail']['PlanId'].unique())
    }
    summary_actions = {
        "Total": len(df),
        "Executed": len(df[df['Result'].notna()]),
        "Pending": len(df[df['Result'].isna()]),
        "Time Taken (s)": round(df['TimeTaken'].sum(), 2),
        "Pass": len(df[df['Result'] == 'Pass']),
        "Fail": len(df[df['Result'] == 'Fail'])
    }

    html_content = """
    <html>
    <head>
        <title>Test Dashboard - {}</title>
        <style>
            table {{ border-collapse: collapse; width: 100%; }}
            th, td {{ border: 1px solid black; padding: 8px; text-align: left; }}
            th {{ background-color: #f2f2f2; }}
        </style>
    </head>
    <body>
        <h1>Test Dashboard - {}</h1>
        <h2>Summary - Plans</h2>
        <table>
            <tr><th>Total</th><td>{}</td></tr>
            <tr><th>Executed</th><td>{}</td></tr>
            <tr><th>Pending</th><td>{}</td></tr>
            <tr><th>Time Taken (s)</th><td>{}</td></tr>
            <tr><th>Pass</th><td>{}</td></tr>
            <tr><th>Fail</th><td>{}</td></tr>
        </table>
        <h2>Summary - Actions</h2>
        <table>
            <tr><th>Total</th><td>{}</td></tr>
            <tr><th>Executed</th><td>{}</td></tr>
            <tr><th>Pending</th><td>{}</td></tr>
            <tr><th>Time Taken (s)</th><td>{}</td></tr>
            <tr><th>Pass</th><td>{}</td></tr>
            <tr><th>Fail</th><td>{}</td></tr>
        </table>
        <h2>Plans</h2>
        <table>
            <tr><th>DesignId</th><th>PlanId</th><th>Output</th><th>Result</th><th>Time (s)</th></tr>
            {}
        </table>
        <h2>Actions</h2>
        <table>
            <tr><th>DesignId</th><th>PlanId</th><th>StepId</th><th>StepInfo</th><th>ActionType</th><th>ActionName</th><th>Input</th><th>Output</th><th>Expected</th><th>Result</th><th>TimeTaken (s)</th></tr>
            {}
        </table>
    </body>
    </html>
    """

    plan_rows = ""
    for _, row in df.groupby(['DesignId', 'PlanId']).first().reset_index().iterrows():
        plan_rows += f"<tr><td>{row['DesignId']}</td><td>{row['PlanId']}</td><td>{row.get('Output', '')}</td><td>{row['Result']}</td><td>{round(row['TimeTaken'], 2)}</td></tr>\n"

    action_rows = ""
    for _, row in df.iterrows():
        action_rows += f"<tr><td>{row['DesignId']}</td><td>{row['PlanId']}</td><td>{row['StepId']}</td><td>{row['StepInfo']}</td><td>{row['ActionType']}</td><td>{row['ActionName']}</td><td>{row['Input']}</td><td>{row['Output']}</td><td>{row.get('Expected', '')}</td><td>{row['Result']}</td><td>{round(row['TimeTaken'], 2)}</td></tr>\n"

    html_content = html_content.format(
        ypad_name, ypad_name,
        summary_plans["Total"], summary_plans["Executed"], summary_plans["Pending"],
        summary_plans["Time Taken (s)"], summary_plans["Pass"], summary_plans["Fail"],
        summary_actions["Total"], summary_actions["Executed"], summary_actions["Pending"],
        summary_actions["Time Taken (s)"], summary_actions["Pass"], summary_actions["Fail"],
        plan_rows, action_rows
    )

    dashboard_path = os.path.join(output_dir, f"{ypad_name}_zDash.html")
    with open(dashboard_path, 'w', encoding='utf-8') as f:
        f.write(html_content)

def process_action(args):
    """Process a single action for a given plan and design.
    args may include optional previous_results (list of result dicts from earlier steps)
    so that placeholders like {{step:S2}} can be replaced with that step's Output.
    """
    plan_id, design_id, action_row, results_dir, timeout, ypad_config = args[:6]
    previous_results = args[6] if len(args) > 6 else []
    step_id = action_row['StepId']
    action_type = action_row['ActionType']
    action_name = action_row['ActionName']
    input_data = str(action_row['Input'])
    expected = str(action_row.get('Expected', ''))
    output = action_row.get('Output', '')
    step_info = action_row.get('StepInfo', '')
    critical = str(action_row.get('Critical', 'n')).strip().lower()

    # Suppress technical logging - only log warnings and errors
    # logger.info(f"Processing action for PlanId={plan_id}")

    # Resolve variables from y3Designs.csv (load all design files and concatenate)
    try:
        y3_designs_list = []
        for design_file in ypad_config['input_files']['yDesigns']:
            try:
                df = load_csv(design_file)
                y3_designs_list.append(df)
            except Exception as e:
                logger.warning(f"Failed to load design file {design_file}: {str(e)}")
        if y3_designs_list:
            y3_designs = pd.concat(y3_designs_list, ignore_index=True)
        else:
            y3_designs = pd.DataFrame()
    except Exception as e:
        # If loading designs fails, log but continue (variables won't be resolved)
        logger.warning(f"Failed to load y3Designs files: {str(e)}")
        y3_designs = pd.DataFrame()
    
    import re
    for col in y3_designs.columns:
        if col not in ['Type', 'DataName']:
            if col == design_id:
                for _, row in y3_designs.iterrows():
                    try:
                        data_name = row['DataName']
                        data_value = str(row[design_id])
                        # Clean the data value: remove leading/trailing quotes only if they wrap the entire value
                        # This handles cases where CSV values have extra outer quotes, but preserves quotes in content
                        data_value = data_value.strip()
                        
                        # Remove outer quotes more aggressively - handle cases where value has quotes inside
                        # Keep removing outer quotes until no more can be removed
                        # This handles: "value", ""value"", """value""", etc.
                        max_iterations = 10  # Prevent infinite loops
                        iteration = 0
                        while iteration < max_iterations and len(data_value) >= 2:
                            iteration += 1
                            original_value = data_value
                            
                            # Check for double quote at start and end
                            if data_value.startswith('"') and data_value.endswith('"'):
                                # Count consecutive quotes at the start and end
                                start_quotes = 0
                                end_quotes = 0
                                for i in range(len(data_value)):
                                    if data_value[i] == '"':
                                        start_quotes += 1
                                    else:
                                        break
                                for i in range(len(data_value) - 1, -1, -1):
                                    if data_value[i] == '"':
                                        end_quotes += 1
                                    else:
                                        break
                                # If we have matching quotes at start and end, remove one layer
                                if start_quotes > 0 and end_quotes > 0 and start_quotes == end_quotes:
                                    data_value = data_value[start_quotes:-end_quotes].strip()
                                    # Continue loop to check if there are more outer quotes
                                    continue
                            
                            # Check for single quote at start and end
                            if data_value.startswith("'") and data_value.endswith("'"):
                                start_quotes = 0
                                end_quotes = 0
                                for i in range(len(data_value)):
                                    if data_value[i] == "'":
                                        start_quotes += 1
                                    else:
                                        break
                                for i in range(len(data_value) - 1, -1, -1):
                                    if data_value[i] == "'":
                                        end_quotes += 1
                                    else:
                                        break
                                if start_quotes > 0 and end_quotes > 0 and start_quotes == end_quotes:
                                    data_value = data_value[start_quotes:-end_quotes].strip()
                                    # Continue loop to check if there are more outer quotes
                                    continue
                            
                            # If no changes were made, break
                            if data_value == original_value:
                                break
                        
                        # Fix CSS selectors: convert double quotes to single quotes in attribute selectors
                        # This fixes issues like: button[onclick="addElement()"] -> button[onclick='addElement()']
                        # Also handles escaped quotes: button[onclick=""addElement()""] -> button[onclick='addElement()']
                        # CSS attribute selectors work better with single quotes inside
                        if data_value.startswith('css==') or '[' in data_value:
                            try:
                                # First, handle escaped double quotes ("" -> ")
                                # This handles cases where CSV has ""addElement()"" which pandas might not fully unescape
                                # Replace all occurrences of "" with " (handle multiple escaped quotes)
                                while '""' in data_value:
                                    data_value = data_value.replace('""', '"')
                                
                                # Pattern to match attribute selectors with double quotes: [attr="value"]
                                # Replace with single quotes: [attr='value']
                                def fix_css_quotes(match):
                                    try:
                                        attr_part = match.group(1)  # The attribute name and = sign
                                        value = match.group(2)  # The value inside double quotes
                                        return f"[{attr_part}'{value}']"
                                    except Exception:
                                        # If regex replacement fails, return original match
                                        return match.group(0)
                                
                                # Match pattern: [attribute="value"] and replace with [attribute='value']
                                # Use try-except to handle any regex errors gracefully
                                # Apply multiple times to handle nested or multiple attribute selectors
                                prev_value = ""
                                while prev_value != data_value:
                                    prev_value = data_value
                                    data_value = re.sub(r'\[([^=]+=)"([^"]+)"\]', fix_css_quotes, data_value)
                            except Exception:
                                # If CSS quote fixing fails, continue with original value
                                # This ensures the code doesn't crash on Linux if regex fails
                                pass
                        
                        # Substitute sensitive placeholders from .env (e.g. OPENWEATHERMAP_API, EMAIL_ID, YOUR_PASSWORD)
                        data_value = _substitute_env_in_value(data_value)
                        
                        # Use word boundary replacement to avoid partial matches
                        # But exclude matches that are part of dot-notation paths (e.g., coord.lat should not replace 'lat')
                        # Match variable name only when it's not preceded by a dot and not followed by a dot
                        pattern = r'(?<!\.)\b' + re.escape(data_name) + r'\b(?!\.)'
                        # Use lambda to avoid regex interpretation of replacement string
                        input_data = re.sub(pattern, lambda m: data_value, input_data)
                        expected = re.sub(pattern, lambda m: data_value, expected)
                    except Exception as e:
                        # If variable resolution fails for one row, log and continue
                        logger.warning(f"Failed to resolve variable {data_name if 'data_name' in locals() else 'unknown'}: {str(e)}")
                        continue

    # Resolve {{step:StepId}} placeholders from previous steps' Output (same plan/design)
    for prev in previous_results:
        if prev.get('PlanId') != plan_id or prev.get('DesignId') != design_id:
            continue
        ref_step_id = str(prev.get('StepId', ''))
        out_val = prev.get('Output', '')
        if ref_step_id and out_val is not None:
            placeholder = '{{step:' + ref_step_id + '}}'
            input_data = input_data.replace(placeholder, str(out_val))
            expected = expected.replace(placeholder, str(out_val))

    # Check cache for repeated actions
    cache_key = f"{plan_id}_{step_id}_{input_data}"
    if cache_key in action_cache:
        result, output, time_taken = action_cache[cache_key]
        return {
            'DesignId': design_id, 'PlanId': plan_id, 'StepId': step_id,
            'StepInfo': step_info, 'ActionType': action_type, 'ActionName': action_name,
            'Input': input_data, 'Output': output, 'Expected': expected,
            'Result': result, 'Time': datetime.now().strftime("%H:%M:%S"), 'TimeTaken': time_taken
        }

    # Handle xReuse by re-running the reused plan's actions
    ui_handler = xActions.UIActionHandler(timeout=timeout)  # Initialize once per plan
    if action_type == "xReuse":
        reused_plan_id = action_name
        # Load all plan files and concatenate
        y1_plans_list = []
        for plan_file in ypad_config['input_files']['yPlans']:
            try:
                df = load_csv(plan_file)
                y1_plans_list.append(df)
            except Exception as e:
                logger.warning(f"Failed to load plan file {plan_file}: {str(e)}")
        y1_plans = pd.concat(y1_plans_list, ignore_index=True) if y1_plans_list else pd.DataFrame()
        
        # Load all action files and concatenate
        y2_actions_list = []
        for action_file in ypad_config['input_files']['yActions']:
            try:
                df = load_csv(action_file)
                y2_actions_list.append(df)
            except Exception as e:
                logger.warning(f"Failed to load action file {action_file}: {str(e)}")
        y2_actions = pd.concat(y2_actions_list, ignore_index=True) if y2_actions_list else pd.DataFrame()
        
        reused_plan = y1_plans[y1_plans['PlanId'] == reused_plan_id]
        if reused_plan.empty:
            raise ValueError(f"Reused plan {reused_plan_id} not found")
        reused_actions = y2_actions[y2_actions['PlanId'] == reused_plan_id]
        
        # Process all reused actions and collect results (from fEngine.py v1 - better reporting)
        reuse_results = []
        for _, reused_action in reused_actions.iterrows():
            reused_args = (reused_plan_id, design_id, reused_action, results_dir, timeout, ypad_config, reuse_results)
            action_result = process_action(reused_args)
            reuse_results.append(action_result)
            if action_result['ActionType'] == "xUI" and action_result['ActionName'] == "xOpenBrowser":
                continue  # Browser already opened by ui_handler
            if action_result['Result'] == "Fail":
                return action_result
        
        # Return success result for xReuse (from fEngine.py v1 - provides better feedback)
        return {
            'DesignId': design_id, 'PlanId': plan_id, 'StepId': step_id,
            'StepInfo': step_info, 'ActionType': action_type, 'ActionName': action_name,
            'Input': input_data, 'Output': f"Successfully reused plan {reused_plan_id} with {len(reuse_results)} actions", 
            'Expected': expected, 'Result': 'Pass', 'Time': datetime.now().strftime("%H:%M:%S"), 'TimeTaken': 0
        }

    # Execute the action (no driver_path logic)
    # Only pass ui_handler for UI actions to maintain browser session
    handler_param = ui_handler if action_type == "xUI" else None
    
    # Add 0-second delay before closing browser to ensure all operations complete (from fEngine2.py v2)
    if action_type == "xUI" and action_name == "xCloseBrowser":
        time.sleep(0)
    
    result, output, time_taken = xActions.runAction(
        action_type, action_name, input_data, output, expected,
        plan_id, design_id, step_id, results_dir, handler=handler_param, timeout=timeout
    )

    # Cache the result
    action_cache[cache_key] = (result, output, time_taken)

    return {
        'DesignId': design_id, 'PlanId': plan_id, 'StepId': step_id,
        'StepInfo': step_info, 'ActionType': action_type, 'ActionName': action_name,
        'Input': input_data, 'Output': output, 'Expected': expected,
        'Critical': critical,
        'Result': result, 'Time': datetime.now().strftime("%H:%M:%S"), 'TimeTaken': time_taken
    }

def process_plan(args):
    """Process a single plan for a given design."""
    plan_row, ypad_config, output_dir, timeout, plan_index, total_plans = args
    plan_id = plan_row['PlanId']
    design_ids = str(plan_row['DesignId']).split(';')
    
    # Load all action files and concatenate
    y2_actions_list = []
    for action_file in ypad_config['input_files']['yActions']:
        try:
            df = load_csv(action_file)
            y2_actions_list.append(df)
        except Exception as e:
            logger.warning(f"Failed to load action file {action_file}: {str(e)}")
    y2_actions = pd.concat(y2_actions_list, ignore_index=True) if y2_actions_list else pd.DataFrame()
    
    actions = y2_actions[y2_actions['PlanId'] == plan_id]
    results = []

    # Show plan execution start
    print_status(f"Starting plan: {plan_id}", "RUNNING")
    
    for design_id in design_ids:
        # Suppress technical logging
        # logger.info(f"Executing PlanId={plan_id} for DesignId={design_id}")
        results_dir = os.path.join(output_dir, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{plan_id}")
        os.makedirs(results_dir, exist_ok=True)

        # Process each action (pass previous results so {{step:StepId}} can be resolved)
        for action_index, (_, action_row) in enumerate(actions.iterrows(), 1):
            action_args = (plan_id, design_id, action_row, results_dir, timeout, ypad_config, results)
            result = process_action(action_args)
            results.append(result)
            
            # Show action progress
            if result['Result'] == 'Pass':
                print_status(f"  ✓ {action_row['StepInfo']}", "SUCCESS")
            elif result['Result'] == 'Fail':
                print_status(f"  ✗ {action_row['StepInfo']} - {result.get('Output', 'Failed')}", "ERROR")
                # If action marked Critical, stop executing remaining actions for this plan/design
                is_critical = str(action_row.get('Critical', 'n')).strip().lower() in {'y', 'yes', 'true', '1'}
                if is_critical:
                    print_status(f"  → Critical step failed. Skipping remaining actions for plan {plan_id} / design {design_id}.", "WARNING")
                    break

    # Show plan completion
    plan_results = [r for r in results if r['PlanId'] == plan_id]
    passed_actions = len([r for r in plan_results if r['Result'] == 'Pass'])
    total_actions = len(plan_results)
    
    if passed_actions == total_actions:
        print_status(f"Plan {plan_id} completed successfully ({passed_actions}/{total_actions} actions)", "SUCCESS")
    else:
        print_status(f"Plan {plan_id} completed with issues ({passed_actions}/{total_actions} actions passed)", "WARNING")

    return results


def _execute_single_ypad_suite(config_index, total_configs, config_path, main_config, debug_mode, timeout, start_time):
    """Run one YPAD (test suite): load plans, execute plans, write results and dashboard."""
    # Each process (including multiprocessing workers) must load .env for y3Designs substitution.
    load_env()

    print_header(f"Processing Test Suite {config_index}/{total_configs}")

    try:
        ypad_config = load_config(config_path)
    except FileNotFoundError:
        print_status(f"yPAD config not found: {config_path}", "ERROR")
        return None
    ypad_name = os.path.splitext(os.path.basename(config_path))[0]
    output_dir = os.path.join(
        _results_z_root(),
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{ypad_name}",
    )
    os.makedirs(output_dir, exist_ok=True)
    if debug_mode:
        os.makedirs(os.path.join(output_dir, "_debug"), exist_ok=True)

    print_status(f"Test Suite: {ypad_name}", "INFO")
    print_status(f"Output Directory: {output_dir}", "INFO")

    # Load plans and filter by Run=Y (load all plan files and concatenate)
    y1_plans_list = []
    for plan_file in ypad_config['input_files']['yPlans']:
        try:
            df = load_csv(plan_file)
            y1_plans_list.append(df)
            print_status(f"Loaded plan file: {os.path.basename(plan_file)} ({len(df)} plans)", "INFO")
        except Exception as e:
            print_status(f"Failed to load plan file {plan_file}: {str(e)}", "ERROR")
    if y1_plans_list:
        y1_plans = pd.concat(y1_plans_list, ignore_index=True)
        print_status(f"Total plans loaded: {len(y1_plans)} from {len(y1_plans_list)} file(s)", "INFO")
    else:
        print_status("No plan files could be loaded", "ERROR")
        return None

    # Check if 'Run' column exists (case-insensitive check)
    run_column = None
    for col in y1_plans.columns:
        cleaned_col = col.strip().strip('"').strip("'")
        if cleaned_col.lower() == 'run':
            run_column = col
            break

    if run_column is None:
        available_columns = ', '.join([f"'{col}'" for col in y1_plans.columns.tolist()])
        print_status(f"Error: 'Run' column not found in y1Plans.csv", "ERROR")
        print_status(f"Available columns: {available_columns}", "ERROR")
        print_status(f"CSV file: {ypad_config['input_files']['yPlans'][0]}", "ERROR")
        print_status(f"Number of columns: {len(y1_plans.columns)}", "ERROR")
        return None

    plans_to_run = y1_plans[y1_plans[run_column] == 'Y']

    tags_config = main_config.get("tags", [])
    if tags_config is None:
        tags_config = []
    elif isinstance(tags_config, str):
        tags_config = [tags_config] if tags_config.strip() else []
    elif not isinstance(tags_config, list):
        tags_config = []

    tags_column = None
    for col in y1_plans.columns:
        cleaned_col = col.strip().strip('"').strip("'")
        if cleaned_col.lower() == 'tags':
            tags_column = col
            break

    if tags_column and tags_config:
        tags_lower = [str(tag).strip().lower() for tag in tags_config if tag]
        if 'all' in tags_lower:
            print_status("Tag filter: 'All' specified - running all plans", "INFO")
        else:
            def tag_matches(row):
                plan_tag = str(row[tags_column]).strip().lower() if pd.notna(row[tags_column]) else ""
                return any(plan_tag == tag_lower for tag_lower in tags_lower)

            plans_to_run = plans_to_run[plans_to_run.apply(tag_matches, axis=1)]
            if len(tags_lower) > 0:
                print_status(f"Tag filter: Running plans with tags: {', '.join(tags_config)}", "INFO")
    elif tags_config and not tags_column:
        print_status("Warning: Tags specified but 'Tags' column not found in y1Plans.csv - running all plans", "WARNING")

    print_status(f"Found {len(plans_to_run)} plans to execute", "INFO")

    if len(plans_to_run) == 0:
        print_status("No plans marked for execution (Run=Y)", "WARNING")
        return None

    all_results = []
    for plan_index, (_, plan_row) in enumerate(plans_to_run.iterrows(), 1):
        plan_args = (plan_row, ypad_config, output_dir, timeout, plan_index, len(plans_to_run))
        results = process_plan(plan_args)
        all_results.extend(results)
        print_progress(plan_index, len(plans_to_run), "plans")

    print()

    print_status("Generating results and dashboard...", "INFO")
    df = pd.DataFrame(all_results)
    df.to_csv(os.path.join(output_dir, f"{ypad_name}_zResults.csv"), index=False)
    generate_dashboard(df, output_dir, ypad_name)

    try:
        removed = cleanup_empty_directories(output_dir)
        if removed > 0:
            print_status(f"Cleaned up {removed} empty directory(ies)", "INFO")
    except Exception as e:
        logger.debug(f"Failed to clean up empty directories: {str(e)}")

    try:
        err_csv = os.path.join(output_dir, "_errors.csv")
        if os.path.exists(err_csv):
            print_status(f"Error summary saved: {err_csv}", "WARNING")
    except Exception:
        pass

    total_plans = len(plans_to_run)
    plan_results = df.groupby('PlanId').agg({
        'Result': lambda x: 'Pass' if (x == 'Pass').all() else 'Fail'
    }).reset_index()

    passed_plans = len(plan_results[plan_results['Result'] == 'Pass'])
    failed_plans = len(plan_results[plan_results['Result'] == 'Fail'])
    total_time = time.time() - start_time

    dashboard_path = os.path.join(output_dir, f"{ypad_name}_zDash.html")

    summary_stats = {
        'total_plans': total_plans,
        'passed': passed_plans,
        'failed': failed_plans,
        'total_time': total_time,
        'output_dir': output_dir,
        'dashboard_path': dashboard_path,
        'ypad_name': ypad_name,
        'results_csv': os.path.join(output_dir, f"{ypad_name}_zResults.csv"),
    }

    print_summary(summary_stats)
    print_status(f"Test suite '{ypad_name}' completed successfully!", "SUCCESS")

    try:
        if hasattr(xActions, 'UIActionHandler'):
            if hasattr(xActions.UIActionHandler, '_shared_driver') and xActions.UIActionHandler._shared_driver:
                try:
                    xActions.UIActionHandler._shared_driver.quit()
                except Exception:
                    pass
                xActions.UIActionHandler._shared_driver = None
            if getattr(xActions.UIActionHandler, '_chrome_user_data_dir', None):
                try:
                    shutil.rmtree(xActions.UIActionHandler._chrome_user_data_dir, ignore_errors=True)
                except Exception:
                    pass
                xActions.UIActionHandler._chrome_user_data_dir = None

            if hasattr(xActions.UIActionHandler, '_thread_local'):
                if hasattr(xActions.UIActionHandler._thread_local, 'driver') and xActions.UIActionHandler._thread_local.driver:
                    try:
                        xActions.UIActionHandler._thread_local.driver.quit()
                    except Exception:
                        pass
                    xActions.UIActionHandler._thread_local.driver = None
    except Exception:
        pass

    return summary_stats


def run_ypad_suite_worker(args):
    """Worker for parallel YPAD runs: capture stdout/stderr so the parent can print in config order."""
    import traceback
    import logging
    config_index, total_configs, config_path, main_config, debug_mode, timeout, start_time = args
    buf = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    try:
        sys.stdout = buf
        sys.stderr = buf
        # Rebind logging to the worker buffer so third-party INFO logs (e.g., webdriver_manager)
        # are captured and replayed in-order with suite output.
        buffer_handler = logging.StreamHandler(buf)
        buffer_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        root_logger.handlers = [buffer_handler]
        if original_level > logging.INFO:
            root_logger.setLevel(logging.INFO)
        _execute_single_ypad_suite(
            config_index, total_configs, config_path, main_config, debug_mode, timeout, start_time
        )
    except Exception:
        traceback.print_exc(file=buf)
    finally:
        root_logger.handlers = original_handlers
        root_logger.setLevel(original_level)
        sys.stdout = old_out
        sys.stderr = old_err
    return config_index, buf.getvalue()


def _normalize_buffered_output(text):
    """Normalize buffered output so replay in parent console is stable."""
    # Keep only the visible segment after the last carriage return on each line.
    normalized = []
    for line in text.replace('\r\n', '\n').split('\n'):
        normalized.append(line.split('\r')[-1] if '\r' in line else line)
    return '\n'.join(normalized)


def _wait_future_with_heartbeat(future, suite_name, config_index, total_configs, interval_sec):
    """
    Block until future completes while printing a live heartbeat on the real console.
    Worker output stays buffered; this only shows that suites are still running.
    """
    bar_len = 24
    tick = 0
    start = time.time()
    while True:
        if future.done():
            sys.stdout.write("\r" + " " * 120 + "\r")
            sys.stdout.flush()
            return future.result()
        elapsed = int(time.time() - start)
        phase = tick % (bar_len + 1)
        bar = "█" * phase + "-" * (bar_len - phase)
        extra = ""
        if total_configs > 1:
            extra = " (parallel workers may be running other suites)"
        line = (
            f"\rProgress: |{bar}| {elapsed}s — waiting for {suite_name} "
            f"(suite {config_index}/{total_configs}){extra}…"
        )
        sys.stdout.write(line)
        sys.stdout.flush()
        tick += 1
        time.sleep(max(0.5, float(interval_sec)))


# --- LLM YPAD build (--build) -------------------------------------------------

OPENAI_MODEL_YPAD_BUILD = "gpt-4o"

# Sample BUILD.md (repo root). Front matter sets ypad_name; body is free-form requirements.
SAMPLE_BUILD_MD = """---
ypad_name: MySuite
---

Describe the automation you need: goals, applications under test, environments, tags, and
any constraints. Use placeholder names in prose for secrets (e.g. MY_API_KEY) — real values
belong in .env and are referenced by name in y3Designs only.
"""


def _resolved_main_config_path_for_write():
    rel = _default_main_config_path()
    if os.path.isabs(rel):
        return rel
    return _resource_path(rel)


def _parse_build_md(content):
    """Parse BUILD.md front matter for ypad_name; return (sanitized_name, body_markdown)."""
    content = content.strip()
    if not content.startswith('---'):
        raise ValueError(
            "BUILD.md must start with YAML front matter. Example:\n" + SAMPLE_BUILD_MD
        )
    lines = content.splitlines()
    if len(lines) < 3 or lines[0].strip() != '---':
        raise ValueError("BUILD.md front matter must start with ---")
    meta = {}
    i = 1
    while i < len(lines) and lines[i].strip() != '---':
        line = lines[i]
        if ':' in line:
            k, _, v = line.partition(':')
            meta[k.strip()] = v.strip().strip('"').strip("'")
        i += 1
    if i >= len(lines) or lines[i].strip() != '---':
        raise ValueError("BUILD.md front matter must end with a line ---")
    body = '\n'.join(lines[i + 1:]).strip()
    name = meta.get('ypad_name') or meta.get('YPAD_NAME')
    if not name:
        raise ValueError(
            "BUILD.md must set ypad_name in front matter. Example:\n" + SAMPLE_BUILD_MD
        )
    return name, body


def _sanitize_ypad_folder_base(name):
    s = re.sub(r'[^\w\-]+', '_', name.strip())
    s = s.strip('_')
    if not s:
        raise ValueError('ypad_name is empty after sanitization')
    if len(s) > 64:
        s = s[:64]
    return s


def _allocate_unique_ypad_folder(base_safe, project_root):
    y_dir = os.path.join(project_root, 'y')
    os.makedirs(y_dir, exist_ok=True)
    if not os.path.exists(os.path.join(y_dir, base_safe)):
        return base_safe
    n = 1
    while os.path.exists(os.path.join(y_dir, f'{base_safe}_{n}')):
        n += 1
    return f'{base_safe}_{n}'


def _validate_generated_ypad_csvs(y1s, y2s, y3s, design_columns, capa_text):
    """Parse and validate CSV strings; raise ValueError on failure."""
    required_y1 = ['PlanId', 'PlanName', 'DesignId', 'Run', 'Tags', 'Output']
    required_y2 = ['PlanId', 'StepId', 'StepInfo', 'ActionType', 'ActionName', 'Input', 'Output', 'Expected', 'Critical']
    try:
        df1 = pd.read_csv(io.StringIO(y1s))
        df2 = pd.read_csv(io.StringIO(y2s))
        df3 = pd.read_csv(io.StringIO(y3s))
    except Exception as e:
        raise ValueError(f'CSV parse failed: {e}') from e
    for col in required_y1:
        if col not in df1.columns:
            raise ValueError(f'y1Plans missing column: {col}')
    for col in required_y2:
        if col not in df2.columns:
            raise ValueError(f'y2Actions missing column: {col}')
    if 'Type' not in df3.columns or 'DataName' not in df3.columns:
        raise ValueError('y3Designs must have Type and DataName columns')
    for dc in design_columns:
        if dc not in df3.columns:
            raise ValueError(f'y3Designs missing design column {dc} (expected {design_columns})')
    allowed = xActions.ypad_build_allowed_actions_from_capa(capa_text)
    ok, err = xActions.ypad_build_validate_y2_against_capa(df2, allowed)
    if not ok:
        raise ValueError(err)
    plan_ids_y1 = set(df1['PlanId'].astype(str))
    plan_ids_y2 = set(df2['PlanId'].astype(str))
    for _, row in df1.iterrows():
        if str(row.get('Run', '')).strip().upper() != 'Y':
            continue
        pid = str(row['PlanId'])
        if pid not in plan_ids_y2:
            raise ValueError(f'Plan {pid} is Run=Y but has no rows in y2Actions')
    for pid in plan_ids_y2:
        if pid not in plan_ids_y1:
            raise ValueError(f'y2Actions references unknown PlanId {pid}')
    return df1, df2, df3


def _call_openai_ypad_build(system_prompt, user_prompt, model=None):
    if OpenAI is None:
        raise RuntimeError('openai package is required. Install with: pip install openai')
    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key:
        raise RuntimeError('OPENAI_API_KEY is not set (add it to .env)')
    client = OpenAI(api_key=api_key)
    use_model = model or OPENAI_MODEL_YPAD_BUILD
    resp = client.chat.completions.create(
        model=use_model,
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        response_format={'type': 'json_object'},
    )
    raw = resp.choices[0].message.content
    if not raw:
        raise RuntimeError('OpenAI returned empty content')
    return json.loads(raw)


def run_ypad_build_from_md():
    """
    Read BUILD.md, call OpenAI, validate CSVs, write y/<name>/y*.csv and y/<name>.json,
    replace fStart.json configs with the new YPAD. Raises on any failure (no partial writes).
    """
    build_path = os.path.join(PROJECT_ROOT, 'BUILD.md')
    if not os.path.isfile(build_path):
        raise FileNotFoundError(f'BUILD.md not found at project root: {build_path}')
    with open(build_path, 'r', encoding='utf-8') as f:
        build_text = f.read()
    ypad_name_raw, body = _parse_build_md(build_text)
    base_safe = _sanitize_ypad_folder_base(ypad_name_raw)
    folder_name = _allocate_unique_ypad_folder(base_safe, PROJECT_ROOT)

    ctx = xActions.ypad_build_llm_context(PROJECT_ROOT)
    capa = ctx['xcapa_csv']
    design_cols = ctx['design_columns']
    dc_list = ', '.join(design_cols)

    system_prompt = f"""You are an expert FoXYiZ YPAD author. Produce automation plans as CSV text.

Output MUST be a single JSON object with exactly these keys:
- "y1_plans_csv" — full CSV string for y1Plans (header + rows)
- "y2_actions_csv" — full CSV string for y2Actions
- "y3_designs_csv" — full CSV string for y3Designs

Rules:
1) y1Plans columns exactly: PlanId, PlanName, DesignId, Run, Tags, Output
   - PlanId starts with P. Run is Y or N. Use semicolon-separated DesignId for multiple designs (e.g. D1;D2).
2) y2Actions columns exactly: PlanId, StepId, StepInfo, ActionType, ActionName, Input, Output, Expected, Critical
   - Critical is y or n (lowercase). StepId is sequential integers per plan.
   - ActionType must be xUI, xMath, xAPI, xJSON, xAI, xSAP, xFile, xEmail, xDB, xLogic, xCloud, xIoT, xTime, xPhone, xReuse, or xCustom.
   - ActionName MUST match the Action column in xCapa.csv for the corresponding Module (xCapa Module UI maps to ActionType xUI; use ONLY names listed in xCapa for that module).
   - For xReuse, ActionName is the PlanId to reuse.
3) y3Designs columns: Type, DataName, then EXACTLY these design columns in order: {dc_list}
   - Use placeholder tokens in cell values for secrets (e.g. MY_API_KEY) — never real secrets.
4) Match the style and linking between the three files like the reference Mix YPAD: every PlanId in y1 with Run=Y must have steps in y2; every Input reference to a design key must exist in y3 DataName.
5) Escape CSV quoting correctly (double quotes inside fields).

Reference design column count from the Mix YPAD: {len(design_cols)} ({dc_list})."""

    user_prompt = f"""## xCapa.csv (authoritative Action names and types)\n\n{capa}\n\n## Reference Mix YPAD — y1Plans.csv\n\n{ctx['mix_y1Plans.csv']}\n\n## Reference Mix YPAD — y2Actions.csv\n\n{ctx['mix_y2Actions.csv']}\n\n## Reference Mix YPAD — y3Designs.csv\n\n{ctx['mix_y3Designs.csv']}\n\n## BUILD.md requirements (body)\n\n{body}\n"""

    print_header("YPAD BUILD (LLM)")
    print_status(f"Target folder: y/{folder_name}/", "INFO")
    print_status(f"Calling OpenAI model {OPENAI_MODEL_YPAD_BUILD}...", "RUNNING")

    data = _call_openai_ypad_build(system_prompt, user_prompt)
    for key in ('y1_plans_csv', 'y2_actions_csv', 'y3_designs_csv'):
        if key not in data:
            raise ValueError(f'OpenAI JSON missing key: {key}')
    y1s = data['y1_plans_csv']
    y2s = data['y2_actions_csv']
    y3s = data['y3_designs_csv']

    print_status("Validating generated CSVs...", "INFO")
    _validate_generated_ypad_csvs(y1s, y2s, y3s, design_cols, capa)

    ypad_dir = os.path.join(PROJECT_ROOT, 'y', folder_name)
    json_rel = f'y/{folder_name}.json'
    json_abs = os.path.join(PROJECT_ROOT, 'y', f'{folder_name}.json')
    cfg_payload = {
        'input_files': {
            'yPlans': [f'y/{folder_name}/y1Plans.csv'],
            'yActions': [f'y/{folder_name}/y2Actions.csv'],
            'yDesigns': [f'y/{folder_name}/y3Designs.csv'],
        }
    }

    try:
        os.makedirs(ypad_dir, exist_ok=True)
        with open(os.path.join(ypad_dir, 'y1Plans.csv'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(y1s if y1s.endswith('\n') else y1s + '\n')
        with open(os.path.join(ypad_dir, 'y2Actions.csv'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(y2s if y2s.endswith('\n') else y2s + '\n')
        with open(os.path.join(ypad_dir, 'y3Designs.csv'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(y3s if y3s.endswith('\n') else y3s + '\n')
        with open(json_abs, 'w', encoding='utf-8') as f:
            json.dump(cfg_payload, f, indent=2)
            f.write('\n')

        main_cfg_path = _resolved_main_config_path_for_write()
        existing = {}
        if os.path.isfile(main_cfg_path):
            with open(main_cfg_path, 'r', encoding='utf-8') as f:
                existing = json.load(f)
        existing['configs'] = [json_rel]
        with open(main_cfg_path, 'w', encoding='utf-8') as f:
            json.dump(existing, f, indent=2)
            f.write('\n')
    except Exception:
        shutil.rmtree(ypad_dir, ignore_errors=True)
        if os.path.isfile(json_abs):
            try:
                os.remove(json_abs)
            except OSError:
                pass
        raise

    print_status(f"Wrote {json_rel} and y/{folder_name}/y1Plans.csv, y2Actions.csv, y3Designs.csv", "SUCCESS")
    print_status(f"Updated main config to run only: {json_rel}", "SUCCESS")


# --- YPAD analyze (--analyze) -------------------------------------------------

OPENAI_MODEL_YPAD_ANALYZE = "gpt-4o"
_MAX_ANALYZE_USER_PROMPT_CHARS = 120000


def _parse_ypad_cli_path(arg):
    """
    Normalize CLI path to a YPAD suite JSON under project root, e.g. y/Cric/ -> y/Cric.json.
    Accepts: y/Cric/, y/Cric, Cric (folder name only). Used by --analyze, --heal, and --loop.
    """
    if not arg or not str(arg).strip():
        raise ValueError("Missing path after --analyze, --heal, or --loop (e.g. y/Cric/)")
    s = str(arg).strip().strip('"').rstrip("/\\").replace("\\", "/")
    # Resolve so y/<name> works even when cwd is not the project root. Frozen exe in f/ uses
    # ../y/ like dev — do not join only PROJECT_ROOT (bundle path), match _resource_path bases.
    if os.path.isabs(s) or (len(s) > 1 and s[1] == ":"):
        _cand = os.path.abspath(s.replace("/", os.sep))
    else:
        _cand = _resolve_cli_relative_path(s)
    if os.path.isdir(_cand):
        root = _data_root_containing(_cand)
        try:
            s = os.path.relpath(os.path.abspath(_cand), root).replace("\\", "/")
        except ValueError:
            pass
    if s.lower().endswith(".json"):
        rel = s
        base = os.path.splitext(os.path.basename(rel))[0]
        json_abs = _resource_path(rel)
        ydir = os.path.join(os.path.dirname(json_abs), base)
        if not os.path.isfile(json_abs):
            raise FileNotFoundError(f"YPAD config not found: {rel}")
        if not os.path.isdir(ydir):
            raise FileNotFoundError(f"YPAD folder not found: y/{base}/")
        return rel, base
    parts = [p for p in s.split("/") if p and p != "."]
    if not parts:
        raise ValueError("Invalid YPAD path")
    if parts[0] != "y":
        if len(parts) != 1:
            raise ValueError("Use y/<folder>/ or the folder name only (e.g. Cric)")
        suite_name = parts[0]
    else:
        if len(parts) < 2:
            raise ValueError("Use y/<folder>/ (e.g. y/Cric/)")
        suite_name = parts[1]
    json_rel = f"y/{suite_name}.json"
    json_abs = _resource_path(json_rel)
    ydir = os.path.join(os.path.dirname(json_abs), suite_name)
    if not os.path.isdir(ydir):
        raise FileNotFoundError(f"YPAD folder not found: y/{suite_name}/")
    if not os.path.isfile(json_abs):
        raise FileNotFoundError(f"YPAD config not found: {json_rel} (create it next to y/{suite_name}/)")
    return json_rel, suite_name


def _truncate_for_analyze_llm(text, max_chars=_MAX_ANALYZE_USER_PROMPT_CHARS):
    if text is None:
        return ""
    text = str(text)
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head - 80
    return (
        text[:head]
        + "\n\n... [truncated middle] ...\n\n"
        + text[-tail:]
    )


def _design_columns_from_y3_config(ypad_cfg):
    """Read D1, D2, ... column names from the first yDesigns file in the YPAD config."""
    import csv

    paths = ypad_cfg.get("input_files", {}).get("yDesigns", [])
    if not paths:
        raise ValueError("YPAD config has no yDesigns input_files")
    p = _resource_path(paths[0])
    with open(p, "r", encoding="utf-8") as f:
        header = next(csv.reader(f))
    cols = [c.strip() for c in header if re.match(r"^D\d+$", c.strip())]
    if not cols:
        raise ValueError(f"No D1,D2,... columns in {paths[0]}")
    return cols


def _gather_ypad_review_context(json_rel, stats):
    """
    Load suite JSON, y1/y2/y3 CSVs, latest zResults, and _errors.csv into one markdown blob.
    stats must be the dict returned by _execute_single_ypad_suite.
    """
    ypad_cfg = load_config(json_rel)
    parts = []
    cfg_abs = _resource_path(json_rel)
    try:
        with open(cfg_abs, "r", encoding="utf-8") as f:
            parts.append(f"## {json_rel}\n\n```json\n{f.read()}\n```\n")
    except OSError as e:
        parts.append(f"## {json_rel}\n\n(read failed: {e})\n")

    for key in ("yPlans", "yActions", "yDesigns"):
        paths = ypad_cfg.get("input_files", {}).get(key, [])
        for p in paths:
            try:
                rp = _resource_path(p)
                with open(rp, "r", encoding="utf-8") as f:
                    parts.append(f"## {p}\n\n```\n{f.read()}\n```\n")
            except OSError as e:
                parts.append(f"## {p}\n\n(read failed: {e})\n")

    results_path = stats.get("results_csv")
    if results_path and os.path.isfile(results_path):
        try:
            with open(results_path, "r", encoding="utf-8") as f:
                parts.append(f"## Latest run results ({os.path.basename(results_path)})\n\n```\n{f.read()}\n```\n")
        except OSError as e:
            parts.append(f"## Latest run results\n\n(read failed: {e})\n")

    err_path = os.path.join(stats.get("output_dir", ""), "_errors.csv")
    if err_path and os.path.isfile(err_path):
        try:
            with open(err_path, "r", encoding="utf-8") as f:
                parts.append(f"## _errors.csv\n\n```\n{f.read()}\n```\n")
        except OSError:
            pass

    return _truncate_for_analyze_llm("\n".join(parts))


def _call_openai_ypad_analyze(system_prompt, user_prompt):
    if OpenAI is None:
        raise RuntimeError("openai package is required. Install with: pip install openai")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set (add it to .env)")
    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=OPENAI_MODEL_YPAD_ANALYZE,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content
    if not raw:
        raise RuntimeError("OpenAI returned empty content")
    return json.loads(raw)


def run_ypad_analyze(ypad_path_arg, main_config_path, debug_from_cli=False):
    """
    Run a single YPAD suite, then send suite CSVs + run results to OpenAI.
    Prints only actionable suggestions to stdout (no file changes).
    Returns 0 on success, 1 on failure.
    """
    load_env()
    json_rel, suite_name = _parse_ypad_cli_path(ypad_path_arg)

    try:
        main_config = load_config(main_config_path)
    except FileNotFoundError:
        print_status(f"Main config not found: {main_config_path}", "ERROR")
        return 1

    print_header("YPAD ANALYZE")
    print_status(f"Suite: {suite_name} ({json_rel})", "INFO")
    print_status(f"Model: {OPENAI_MODEL_YPAD_ANALYZE}", "INFO")

    timeout = int(main_config.get("timeout", 6))
    debug_mode = bool(debug_from_cli or main_config.get("debug", False))
    headless_mode = bool(main_config.get("headless", False))
    if headless_mode:
        os.environ["FOXYIZ_HEADLESS"] = "true"
    else:
        os.environ["FOXYIZ_HEADLESS"] = "false"

    try:
        if hasattr(xActions, "set_debug_mode"):
            xActions.set_debug_mode(debug_mode)
    except Exception:
        pass

    start_time = time.time()
    stats = _execute_single_ypad_suite(1, 1, json_rel, main_config, debug_mode, timeout, start_time)
    if not stats:
        print_status("Suite run did not produce results; skipping LLM analysis.", "ERROR")
        return 1

    user_blob = _gather_ypad_review_context(json_rel, stats)

    system_prompt = """You are an expert FoXYiZ YPAD reviewer. You receive the suite JSON, y1/y2/y3 CSVs, and the latest execution results (per-step outputs).

Internally reason about failures, flaky steps, and design issues. Respond with a single JSON object and ONLY this key:
- "suggestions" — array of strings; each is one concrete, actionable improvement to the YPAD (plans, steps, locators, Expected values, Critical flags, design data, tags, timeouts, or structure).

Rules:
- Do not include an "observations" key in the JSON.
- Do not repeat the raw CSVs back.
- If the suite is already strong, return a short list (e.g. one item about monitoring or maintenance) or an empty array."""

    user_prompt = f"""## Suite name: {suite_name}

## Run summary (from engine)
- Total plans: {stats.get("total_plans")}
- Passed: {stats.get("passed")}
- Failed: {stats.get("failed")}
- Results directory: {stats.get("output_dir")}
- Dashboard: {stats.get("dashboard_path")}

## YPAD files and results

{user_blob}
"""

    print_status("Calling OpenAI for suggestions...", "RUNNING")
    try:
        data = _call_openai_ypad_analyze(system_prompt, user_prompt)
    except Exception as e:
        print_status(f"OpenAI analysis failed: {e}", "ERROR")
        return 1

    suggestions = data.get("suggestions")
    if suggestions is None:
        print_status('OpenAI JSON missing "suggestions" key.', "ERROR")
        return 1
    if not isinstance(suggestions, list):
        print_status('"suggestions" must be an array of strings.', "ERROR")
        return 1

    print_header("YPAD ANALYSIS — SUGGESTIONS")
    if not suggestions:
        print("No suggestions.")
    else:
        for i, item in enumerate(suggestions, 1):
            line = str(item).strip() if item is not None else ""
            if line:
                print(f"{i}. {line}")
    print()
    return 0


OPENAI_MODEL_YPAD_HEAL = "gpt-4o"


def _normalize_heal_newlines(s):
    return str(s).replace("\r\n", "\n").replace("\r", "\n")


def _pad_csv_disk_form(s):
    """Normalize line endings and trailing newline the same way heal writes files."""
    t = _normalize_heal_newlines(str(s))
    return t if t.endswith("\n") else t + "\n"


def _print_heal_file_diffs(ypad_cfg, y1s, y2s, y3s):
    """Read current files from disk, compare to healed content, print unified diffs to stdout."""
    yp = ypad_cfg.get("input_files", {})
    plans = yp.get("yPlans") or []
    actions = yp.get("yActions") or []
    designs = yp.get("yDesigns") or []
    mapping = [
        (plans[0], y1s, "y1Plans"),
        (actions[0], y2s, "y2Actions"),
        (designs[0], y3s, "y3Designs"),
    ]
    print_header("HEAL — FILE CHANGES")
    any_change = False
    for rel, new_raw, label in mapping:
        new_form = _pad_csv_disk_form(new_raw)
        abs_path = _resource_path(rel)
        if os.path.isfile(abs_path):
            with open(abs_path, "r", encoding="utf-8") as f:
                old_form = _pad_csv_disk_form(f.read())
        else:
            old_form = ""
        if old_form == new_form:
            print_status(f"{rel} — no changes", "INFO")
            continue
        any_change = True
        print()
        print(f"  {rel}  ({label})")
        print("  " + "-" * 56)
        old_lines = old_form.splitlines(True)
        new_lines = new_form.splitlines(True)
        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            n=3,
        )
        for line in diff:
            sys.stdout.write(line)
            if line and not line.endswith("\n"):
                sys.stdout.write("\n")
    if not any_change:
        print()
        print_status("Healed content matches files on disk (no edits).", "INFO")
    print()


def _write_heal_ypad_csvs(ypad_cfg, y1s, y2s, y3s):
    """Overwrite the first yPlans, yActions, yDesigns paths in the suite config."""
    yp = ypad_cfg.get("input_files", {})
    plans = yp.get("yPlans") or []
    actions = yp.get("yActions") or []
    designs = yp.get("yDesigns") or []
    if not plans or not actions or not designs:
        raise ValueError("YPAD config must list yPlans, yActions, and yDesigns")
    mapping = [
        (plans[0], y1s),
        (actions[0], y2s),
        (designs[0], y3s),
    ]
    for rel, text in mapping:
        abs_path = _resource_path(rel)
        parent = os.path.dirname(abs_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(abs_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(_pad_csv_disk_form(text))


def _ypad_suite_stats_all_passed(stats):
    """True if the last suite run has at least one plan and zero failed plans."""
    if not stats:
        return False
    try:
        total = int(stats.get("total_plans", 0))
        failed = int(stats.get("failed", -1))
    except (TypeError, ValueError):
        return False
    return total > 0 and failed == 0


def _heal_apply_from_last_run(json_rel, suite_name, stats, round_label=None):
    """
    OpenAI heal + validate + diff + write. Caller must have run the suite already (stats).
    Returns 0 on success, 1 on failure.
    """
    ypad_cfg = load_config(json_rel)
    try:
        design_cols = _design_columns_from_y3_config(ypad_cfg)
    except ValueError as e:
        print_status(f"Invalid YPAD design files: {e}", "ERROR")
        return 1

    dc_list = ", ".join(design_cols)
    ctx = xActions.ypad_build_llm_context(PROJECT_ROOT)
    capa = ctx["xcapa_csv"]
    user_blob = _gather_ypad_review_context(json_rel, stats)

    system_prompt = f"""You are an expert FoXYiZ YPAD author. A test run just finished; your job is to return UPDATED y1Plans, y2Actions, and y3Designs CSVs that fix failures (wrong locators, Expected strings, Critical flags, design data, tags, or step order) while keeping the suite coherent.

Output MUST be a single JSON object with exactly these keys:
- "y1_plans_csv" — full CSV string for y1Plans (header + rows)
- "y2_actions_csv" — full CSV string for y2Actions
- "y3_designs_csv" — full CSV string for y3Designs

Rules:
1) y1Plans columns exactly: PlanId, PlanName, DesignId, Run, Tags, Output
   - PlanId starts with P. Run is Y or N. Use semicolon-separated DesignId for multiple designs (e.g. D1;D2).
2) y2Actions columns exactly: PlanId, StepId, StepInfo, ActionType, ActionName, Input, Output, Expected, Critical
   - Critical is y or n (lowercase). StepId is sequential integers per plan.
   - ActionType must be xUI, xMath, xAPI, xJSON, xAI, xSAP, xFile, xEmail, xDB, xLogic, xCloud, xIoT, xTime, xPhone, xReuse, or xCustom.
   - ActionName MUST match the Action column in xCapa.csv for the corresponding Module (xCapa Module UI maps to ActionType xUI; use ONLY names listed in xCapa for that module).
   - For xReuse, ActionName is the PlanId to reuse.
3) y3Designs columns: Type, DataName, then EXACTLY these design columns in order: {dc_list}
   - Preserve placeholder tokens for secrets (e.g. MY_API_KEY) — never invent real secrets.
4) Every PlanId in y1 with Run=Y must have steps in y2; every Input reference to a design key must exist in y3 DataName.
5) Escape CSV quoting correctly (double quotes inside fields).
6) Prefer minimal edits: fix what broke; do not rename plans unless necessary.

Design columns for this suite (must match y3): {len(design_cols)} ({dc_list})."""

    user_prompt = f"""## xCapa.csv (authoritative Action names and types)

{capa}

## Reference Mix YPAD — y1Plans.csv

{ctx["mix_y1Plans.csv"]}

## Reference Mix YPAD — y2Actions.csv

{ctx["mix_y2Actions.csv"]}

## Reference Mix YPAD — y3Designs.csv

{ctx["mix_y3Designs.csv"]}

## Suite name: {suite_name}

## Run summary (from engine)
- Total plans: {stats.get("total_plans")}
- Passed: {stats.get("passed")}
- Failed: {stats.get("failed")}
- Results directory: {stats.get("output_dir")}
- Dashboard: {stats.get("dashboard_path")}

## Current YPAD files and run results (replace with improved CSVs)

{user_blob}
"""

    label = f" ({round_label})" if round_label else ""
    print_status(f"Calling OpenAI to heal YPAD CSVs{label}...", "RUNNING")
    try:
        data = _call_openai_ypad_build(system_prompt, user_prompt, OPENAI_MODEL_YPAD_HEAL)
    except Exception as e:
        print_status(f"OpenAI heal failed: {e}", "ERROR")
        return 1

    for key in ("y1_plans_csv", "y2_actions_csv", "y3_designs_csv"):
        if key not in data:
            print_status(f'OpenAI JSON missing key: {key}', "ERROR")
            return 1

    y1s = data["y1_plans_csv"]
    y2s = data["y2_actions_csv"]
    y3s = data["y3_designs_csv"]

    print_status("Validating healed CSVs...", "INFO")
    try:
        _validate_generated_ypad_csvs(y1s, y2s, y3s, design_cols, capa)
    except ValueError as e:
        print_status(f"Validation failed (files not written): {e}", "ERROR")
        return 1

    _print_heal_file_diffs(ypad_cfg, y1s, y2s, y3s)

    try:
        _write_heal_ypad_csvs(ypad_cfg, y1s, y2s, y3s)
    except OSError as e:
        print_status(f"Failed to write CSVs: {e}", "ERROR")
        return 1

    print_status(
        "Healed YPAD written: y1Plans, y2Actions, y3Designs (per suite JSON paths).",
        "SUCCESS",
    )
    return 0


def run_ypad_heal(ypad_path_arg, main_config_path, debug_from_cli=False):
    """
    Run the YPAD suite, send suite CSVs + results to OpenAI, validate returned CSVs, and
    overwrite y1/y2/y3 files in place. Does not modify the suite JSON path list.
    Returns 0 on success, 1 on failure.
    """
    load_env()
    json_rel, suite_name = _parse_ypad_cli_path(ypad_path_arg)

    try:
        main_config = load_config(main_config_path)
    except FileNotFoundError:
        print_status(f"Main config not found: {main_config_path}", "ERROR")
        return 1

    print_header("YPAD HEAL")
    print_status(f"Suite: {suite_name} ({json_rel})", "INFO")
    print_status(f"Model: {OPENAI_MODEL_YPAD_HEAL}", "INFO")

    timeout = int(main_config.get("timeout", 6))
    debug_mode = bool(debug_from_cli or main_config.get("debug", False))
    headless_mode = bool(main_config.get("headless", False))
    if headless_mode:
        os.environ["FOXYIZ_HEADLESS"] = "true"
    else:
        os.environ["FOXYIZ_HEADLESS"] = "false"

    try:
        if hasattr(xActions, "set_debug_mode"):
            xActions.set_debug_mode(debug_mode)
    except Exception:
        pass

    start_time = time.time()
    stats = _execute_single_ypad_suite(1, 1, json_rel, main_config, debug_mode, timeout, start_time)
    if not stats:
        print_status("Suite run did not produce results; cannot heal.", "ERROR")
        return 1

    return _heal_apply_from_last_run(json_rel, suite_name, stats)


def run_ypad_loop(ypad_path_arg, main_config_path, debug_from_cli=False):
    """
    Run suite → if not all plans passed, heal → repeat up to 3 times.
    Stops early when all plans pass. Requires OPENAI_API_KEY for heal steps.
    Returns 0 if all plans pass within the loop, 1 otherwise.
    """
    load_env()
    json_rel, suite_name = _parse_ypad_cli_path(ypad_path_arg)

    try:
        main_config = load_config(main_config_path)
    except FileNotFoundError:
        print_status(f"Main config not found: {main_config_path}", "ERROR")
        return 1

    print_header("YPAD LOOP")
    print_status(f"Suite: {suite_name} ({json_rel})", "INFO")
    print_status(
        "Up to 3 heal rounds: run suite → if any plan fails, heal → repeat; then a final run to verify.",
        "INFO",
    )
    print_status(f"Heal model: {OPENAI_MODEL_YPAD_HEAL}", "INFO")

    timeout = int(main_config.get("timeout", 6))
    debug_mode = bool(debug_from_cli or main_config.get("debug", False))
    headless_mode = bool(main_config.get("headless", False))
    if headless_mode:
        os.environ["FOXYIZ_HEADLESS"] = "true"
    else:
        os.environ["FOXYIZ_HEADLESS"] = "false"

    try:
        if hasattr(xActions, "set_debug_mode"):
            xActions.set_debug_mode(debug_mode)
    except Exception:
        pass

    for round_idx in range(3):
        print_header(f"YPAD LOOP — round {round_idx + 1} / 3")
        start_time = time.time()
        stats = _execute_single_ypad_suite(1, 1, json_rel, main_config, debug_mode, timeout, start_time)
        if not stats:
            print_status("Suite run did not produce results.", "ERROR")
            return 1

        if _ypad_suite_stats_all_passed(stats):
            print_status("All plans passed. Stopping loop.", "SUCCESS")
            return 0

        print_status(
            f"Not all plans passed (failed: {stats.get('failed', '?')}). Running heal...",
            "WARNING",
        )
        rc = _heal_apply_from_last_run(
            json_rel,
            suite_name,
            stats,
            round_label=f"round {round_idx + 1}/3",
        )
        if rc != 0:
            return rc

    print_header("YPAD LOOP — verify run")
    start_time = time.time()
    stats = _execute_single_ypad_suite(1, 1, json_rel, main_config, debug_mode, timeout, start_time)
    if not stats:
        print_status("Verify run did not produce results.", "ERROR")
        return 1
    if _ypad_suite_stats_all_passed(stats):
        print_status("All plans passed after heal loop.", "SUCCESS")
        return 0

    print_status(
        "Still not all plans passed after 3 heal rounds (see latest z/ results).",
        "ERROR",
    )
    return 1


def run_framework_execution(config_path, debug_from_cli=False):
    """Load main config and run all YPAD suites. Returns 0 on success, 2 if config missing."""
    print_header("FoXYiZ Test Framework")
    print_status("Loading configuration...", "INFO")

    env_path = _env_path()
    if env_path:
        load_env()
        print_status(f"Loaded .env from {os.path.dirname(env_path)}", "INFO")
    else:
        load_env()

    try:
        main_config = load_config(config_path)
    except FileNotFoundError:
        print_status(f"Main config not found: {config_path}", "ERROR")
        print_status(
            "Ensure fStart.json is next to the executable, or f/fStart.json if the exe is at project root (or pass --config).",
            "ERROR",
        )
        return 2
    configs = main_config.get("configs", [])
    timeout = main_config.get("timeout", 6)
    debug_mode = bool(debug_from_cli or main_config.get("debug", False))
    headless_mode = bool(main_config.get("headless", False))

    if headless_mode:
        os.environ['FOXYIZ_HEADLESS'] = 'true'
        print_status("Headless mode enabled", "INFO")
    else:
        os.environ['FOXYIZ_HEADLESS'] = 'false'
        print_status("Headless mode disabled - browsers will open (cloud auto-detection enabled)", "INFO")

    try:
        if hasattr(xActions, 'set_debug_mode'):
            xActions.set_debug_mode(debug_mode)
    except Exception:
        pass

    max_threads = min(multiprocessing.cpu_count(), 4)
    thread_count = int(main_config.get("thread_count", max_threads))
    print_status(f"Using {thread_count} threads for parallel execution", "INFO")

    start_time = time.time()

    total_configs = len(configs)
    suite_args = [
        (i, total_configs, path, main_config, debug_mode, timeout, start_time)
        for i, path in enumerate(configs, 1)
    ]

    if total_configs > 1 and thread_count > 1:
        workers = min(thread_count, total_configs)
        heartbeat_interval = main_config.get("heartbeat_interval", 3)
        try:
            heartbeat_interval = float(heartbeat_interval)
        except (TypeError, ValueError):
            heartbeat_interval = 3.0
        if heartbeat_interval < 0.5:
            heartbeat_interval = 0.5
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures_by_index = {
                idx: executor.submit(run_ypad_suite_worker, suite_args[idx - 1])
                for idx in range(1, total_configs + 1)
            }
            for idx in range(1, total_configs + 1):
                config_path = suite_args[idx - 1][2]
                suite_name = os.path.splitext(os.path.basename(config_path))[0]
                _, captured = _wait_future_with_heartbeat(
                    futures_by_index[idx],
                    suite_name,
                    idx,
                    total_configs,
                    heartbeat_interval,
                )
                sys.stdout.write(_normalize_buffered_output(captured))
                sys.stdout.flush()
    else:
        for sargs in suite_args:
            _execute_single_ypad_suite(*sargs)
    return 0


def main():
    """Main function to execute the test framework."""
    # Clear action cache to ensure fresh execution
    global action_cache
    action_cache.clear()
    
    # Kill any leftover chromedriver processes from previous executions
    kill_chromedriver_processes()
    
    # Clean up any leftover browser drivers from previous executions
    try:
        if hasattr(xActions, 'UIActionHandler'):
            # Clean up shared driver
            if hasattr(xActions.UIActionHandler, '_shared_driver') and xActions.UIActionHandler._shared_driver:
                try:
                    xActions.UIActionHandler._shared_driver.quit()
                except Exception:
                    pass
                xActions.UIActionHandler._shared_driver = None
            if getattr(xActions.UIActionHandler, '_chrome_user_data_dir', None):
                try:
                    shutil.rmtree(xActions.UIActionHandler._chrome_user_data_dir, ignore_errors=True)
                except Exception:
                    pass
                xActions.UIActionHandler._chrome_user_data_dir = None
            
            # Clean up thread-local driver if it exists
            if hasattr(xActions.UIActionHandler, '_thread_local'):
                if hasattr(xActions.UIActionHandler._thread_local, 'driver') and xActions.UIActionHandler._thread_local.driver:
                    try:
                        xActions.UIActionHandler._thread_local.driver.quit()
                    except Exception:
                        pass
                    xActions.UIActionHandler._thread_local.driver = None
    except Exception:
        pass  # Don't fail if cleanup fails
    
    parser = argparse.ArgumentParser(description="FoXYiZ Test Framework")
    parser.add_argument(
        '--config',
        required=False,
        default=_default_main_config_path(),
        help="Path to the main config JSON file (default: f/fStart.json in dev; fStart.json or f/fStart.json next to exe when frozen)",
    )
    parser.add_argument('--debug', action='store_true', help="Enable verbose debug logging and error artifacts")
    parser.add_argument(
        '--build',
        action='store_true',
        help='Read BUILD.md, generate YPAD CSVs via OpenAI, update fStart.json, then run tests',
    )
    parser.add_argument(
        '--analyze',
        metavar='YPAD_PATH',
        default=None,
        help='Run YPAD at y/<name>/, send suite CSVs + run results to OpenAI; print suggestions only (no file changes)',
    )
    parser.add_argument(
        '--heal',
        metavar='YPAD_PATH',
        default=None,
        help='Run YPAD, send suite + results to OpenAI, validate and overwrite y1/y2/y3 CSVs in place',
    )
    parser.add_argument(
        '--loop',
        metavar='YPAD_PATH',
        default=None,
        help='Run suite and heal up to 3 times; stop early when all plans pass; final verify run after heals',
    )
    args = parser.parse_args()

    _mode_flags = sum(1 for x in (args.build, args.analyze, args.heal, args.loop) if x)
    if _mode_flags > 1:
        print_status('Use only one of --build, --analyze, --heal, or --loop.', 'ERROR')
        return 1

    if args.build:
        load_env()
        if not os.environ.get('OPENAI_API_KEY'):
            print_status('OPENAI_API_KEY is not set. Add it to .env at the project root.', 'ERROR')
            return 1
        try:
            run_ypad_build_from_md()
        except Exception as e:
            print_status(f'Build failed: {e}', 'ERROR')
            return 1
        return run_framework_execution(_default_main_config_path(), args.debug)

    if args.analyze:
        load_env()
        if not os.environ.get('OPENAI_API_KEY'):
            print_status('OPENAI_API_KEY is not set. Add it to .env at the project root.', 'ERROR')
            return 1
        try:
            return run_ypad_analyze(args.analyze, args.config, args.debug)
        except Exception as e:
            print_status(f'Analyze failed: {e}', 'ERROR')
            return 1

    if args.heal:
        load_env()
        if not os.environ.get('OPENAI_API_KEY'):
            print_status('OPENAI_API_KEY is not set. Add it to .env at the project root.', 'ERROR')
            return 1
        try:
            return run_ypad_heal(args.heal, args.config, args.debug)
        except Exception as e:
            print_status(f'Heal failed: {e}', 'ERROR')
            return 1

    if args.loop:
        load_env()
        if not os.environ.get('OPENAI_API_KEY'):
            print_status('OPENAI_API_KEY is not set. Add it to .env at the project root.', 'ERROR')
            return 1
        try:
            return run_ypad_loop(args.loop, args.config, args.debug)
        except Exception as e:
            print_status(f'Loop failed: {e}', 'ERROR')
            return 1

    return run_framework_execution(args.config, args.debug)

if __name__ == "__main__":
    # Multiprocessing support for Windows and PyInstaller
    # Freeze support must be called first for PyInstaller executables
    try:
        multiprocessing.freeze_support()
    except Exception:
        pass  # Not running as frozen executable, continue normally
    
    # Set start method to 'spawn' on Windows for better compatibility
    # This must be called before any multiprocessing operations
    try:
        if sys.platform == 'win32':
            multiprocessing.set_start_method('spawn', force=True)
    except RuntimeError:
        # Start method already set, ignore
        pass
    except Exception:
        # Other platforms or errors, continue
        pass
    
    # YPAD suites may run in parallel worker processes; output is replayed in config order.
    
    try:
        rc = main()
        if rc:
            sys.exit(rc)
    except KeyboardInterrupt:
        print_status("Execution interrupted by user.", "WARNING")
        try:
            # Attempt graceful cleanup of shared UI driver if present
            if hasattr(xActions, 'UIActionHandler') and getattr(xActions.UIActionHandler, '_shared_driver', None):
                try:
                    xActions.UIActionHandler._shared_driver.quit()
                except Exception:
                    pass
                xActions.UIActionHandler._shared_driver = None
            if hasattr(xActions, 'UIActionHandler') and getattr(xActions.UIActionHandler, '_chrome_user_data_dir', None):
                try:
                    shutil.rmtree(xActions.UIActionHandler._chrome_user_data_dir, ignore_errors=True)
                except Exception:
                    pass
                xActions.UIActionHandler._chrome_user_data_dir = None
        except Exception:
            pass
        sys.exit(130)