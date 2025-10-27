import os
import json
import time
import pandas as pd
import argparse
import logging
import sys
from datetime import datetime
from multiprocessing import Pool
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
        x_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'x'))
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
    print(f"\rProgress: |{bar}| {percentage:.1f}% ({current}/{total} {item_name})", end='', flush=True)

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

# Global cache for action results
action_cache = {}

def _resource_path(relative_path):
    """Get absolute path to resource, works for dev and PyInstaller."""
    try:
        base_path = sys._MEIPASS  # type: ignore[attr-defined]
    except Exception:
        base_path = os.path.abspath(os.path.dirname(sys.executable)) if getattr(sys, 'frozen', False) else os.path.abspath(os.path.dirname(__file__))
    return os.path.abspath(os.path.join(base_path, relative_path))

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
    """Load CSV file into a DataFrame."""
    resolved = file_path
    if not os.path.isabs(file_path):
        resolved = _resource_path(file_path)
    df = pd.read_csv(resolved)
    # Fix PlanId column to be string if it exists
    if 'PlanId' in df.columns:
        # Only add 'P' prefix if it's not already there
        df['PlanId'] = df['PlanId'].astype(str)
        df['PlanId'] = df['PlanId'].apply(lambda x: x if x.startswith('P') else 'P' + x)
    return df

def generate_dashboard(df, output_dir, ypad_name):
    """Generate HTML dashboard from results DataFrame."""
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
    # Suppress technical logging
    # logger.info(f"Dashboard generated at {dashboard_path}")

def process_action(args):
    """Process a single action for a given plan and design."""
    plan_id, design_id, action_row, results_dir, timeout, ypad_config = args
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

    # Resolve variables from y3Designs.csv
    y3_designs = load_csv(ypad_config['input_files']['yDesigns'][0])
    for col in y3_designs.columns:
        if col not in ['Type', 'DataName']:
            if col == design_id:
                for _, row in y3_designs.iterrows():
                    data_name = row['DataName']
                    data_value = str(row[design_id])
                    # Use word boundary replacement to avoid partial matches
                    import re
                    pattern = r'\b' + re.escape(data_name) + r'\b'
                    # Use lambda to avoid regex interpretation of replacement string
                    input_data = re.sub(pattern, lambda m: data_value, input_data)
                    expected = re.sub(pattern, lambda m: data_value, expected)

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
        y1_plans = load_csv(ypad_config['input_files']['yPlans'][0])
        y2_actions = load_csv(ypad_config['input_files']['yActions'][0])
        reused_plan = y1_plans[y1_plans['PlanId'] == reused_plan_id]
        if reused_plan.empty:
            raise ValueError(f"Reused plan {reused_plan_id} not found")
        reused_actions = y2_actions[y2_actions['PlanId'] == reused_plan_id]
        for _, reused_action in reused_actions.iterrows():
            reused_args = (reused_plan_id, design_id, reused_action, results_dir, timeout, ypad_config)
            action_result = process_action(reused_args)
            if action_result['ActionType'] == "xUI" and action_result['ActionName'] == "xOpenBrowser":
                continue  # Browser already opened by ui_handler
            if action_result['Result'] == "Fail":
                return action_result

    # Execute the action (no driver_path logic)
    # Only pass ui_handler for UI actions to maintain browser session
    handler_param = ui_handler if action_type == "xUI" else None
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
    y2_actions = load_csv(ypad_config['input_files']['yActions'][0])
    actions = y2_actions[y2_actions['PlanId'] == plan_id]
    results = []

    # Show plan execution start
    print_status(f"Starting plan: {plan_id}", "RUNNING")
    
    for design_id in design_ids:
        # Suppress technical logging
        # logger.info(f"Executing PlanId={plan_id} for DesignId={design_id}")
        results_dir = os.path.join(output_dir, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{plan_id}")
        os.makedirs(results_dir, exist_ok=True)

        # Process each action
        for action_index, (_, action_row) in enumerate(actions.iterrows(), 1):
            action_args = (plan_id, design_id, action_row, results_dir, timeout, ypad_config)
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

def main():
    """Main function to execute the test framework."""
    # Clear action cache to ensure fresh execution
    global action_cache
    action_cache.clear()
    
    parser = argparse.ArgumentParser(description="FoXYiZ Test Framework")
    parser.add_argument('--config', required=False, default='fStart.json', help="Path to the main config JSON file")
    parser.add_argument('--debug', action='store_true', help="Enable verbose debug logging and error artifacts")
    args = parser.parse_args()

    # Show startup banner
    print_header("FoXYiZ Test Framework")
    print_status("Loading configuration...", "INFO")
    
    # Load main config
    # Resolve default config if not provided
    try:
        main_config = load_config(args.config)
    except FileNotFoundError:
        print_status(f"Main config not found: {args.config}", "ERROR")
        print_status("Ensure 'fStart.json' is present next to the executable or pass --config.", "ERROR")
        return 2
    configs = main_config.get("configs", [])
    timeout = main_config.get("timeout", 6)
    debug_mode = bool(args.debug or main_config.get("debug", False))

    # propagate debug mode into action layer
    try:
        if hasattr(xActions, 'set_debug_mode'):
            xActions.set_debug_mode(debug_mode)
    except Exception:
        pass

    # Dynamically adjust thread count based on CPU cores, capped at 4
    import multiprocessing
    max_threads = min(multiprocessing.cpu_count(), 4)
    thread_count = int(main_config.get("thread_count", max_threads))
    print_status(f"Using {thread_count} threads for parallel execution", "INFO")

    start_time = time.time()
    
    for config_index, config_path in enumerate(configs, 1):
        print_header(f"Processing Test Suite {config_index}/{len(configs)}")
        
        try:
            ypad_config = load_config(config_path)
        except FileNotFoundError:
            print_status(f"yPAD config not found: {config_path}", "ERROR")
            continue
        ypad_name = os.path.splitext(os.path.basename(config_path))[0]
        output_dir = os.path.join("z", f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{ypad_name}")
        os.makedirs(output_dir, exist_ok=True)
        if debug_mode:
            os.makedirs(os.path.join(output_dir, "_debug"), exist_ok=True)
        
        print_status(f"Test Suite: {ypad_name}", "INFO")
        print_status(f"Output Directory: {output_dir}", "INFO")

        # Load plans and filter by Run=Y
        y1_plans = load_csv(ypad_config['input_files']['yPlans'][0])
        plans_to_run = y1_plans[y1_plans['Run'] == 'Y']
        
        print_status(f"Found {len(plans_to_run)} plans to execute", "INFO")
        
        if len(plans_to_run) == 0:
            print_status("No plans marked for execution (Run=Y)", "WARNING")
            continue

        # Process plans sequentially for better user experience
        all_results = []
        for plan_index, (_, plan_row) in enumerate(plans_to_run.iterrows(), 1):
            plan_args = (plan_row, ypad_config, output_dir, timeout, plan_index, len(plans_to_run))
            results = process_plan(plan_args)
            all_results.extend(results)
            
            # Show progress
            print_progress(plan_index, len(plans_to_run), "plans")

        print()  # New line after progress bar

        # Generate results and dashboard
        print_status("Generating results and dashboard...", "INFO")
        df = pd.DataFrame(all_results)
        df.to_csv(os.path.join(output_dir, f"{ypad_name}_zResults.csv"), index=False)
        generate_dashboard(df, output_dir, ypad_name)

        # Optional: error summary presence
        try:
            err_csv = os.path.join(output_dir, "_errors.csv")
            if os.path.exists(err_csv):
                print_status(f"Error summary saved: {err_csv}", "WARNING")
        except Exception:
            pass
        
        # Calculate and show summary
        total_plans = len(plans_to_run)
        passed_plans = len(df.groupby(['DesignId', 'PlanId']).first().reset_index()[df.groupby(['DesignId', 'PlanId']).first().reset_index()['Result'] == 'Pass'])
        failed_plans = total_plans - passed_plans
        total_time = time.time() - start_time
        
        dashboard_path = os.path.join(output_dir, f"{ypad_name}_zDash.html")
        
        summary_stats = {
            'total_plans': total_plans,
            'passed': passed_plans,
            'failed': failed_plans,
            'total_time': total_time,
            'output_dir': output_dir,
            'dashboard_path': dashboard_path
        }
        
        print_summary(summary_stats)
        print_status(f"Test suite '{ypad_name}' completed successfully!", "SUCCESS")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print_status("Execution interrupted by user.", "WARNING")
        try:
            # Attempt graceful cleanup of shared UI driver if present
            if hasattr(xActions, 'UIActionHandler') and getattr(xActions.UIActionHandler, '_shared_driver', None):
                try:
                    xActions.UIActionHandler._shared_driver.quit()
                except Exception:
                    pass
        except Exception:
            pass
        sys.exit(130)