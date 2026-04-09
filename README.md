# FoXYiZ Test Framework

A **data-driven test automation framework** that runs test plans defined in CSV (or Excel/JSON). It supports **UI** (Selenium), **API**, **Math**, **AI**, **File**, **Email**, **DB**, **Logic**, **Cloud**, **IoT**, **Time**, **Phone**, and **custom** actions. Plans can reuse other plans, and variables are resolved from design CSV files per environment (DesignId).

---

## Table of Contents

- [Quick Start](#quick-start)
- [Project Structure](#project-structure)
- [Configuration](#configuration)
- [Test Data (Plans, Actions, Designs)](#test-data-plans-actions-designs)
- [Action Types & Capabilities](#action-types--capabilities)
- [Running Tests](#running-tests)
- [Outputs & Dashboard](#outputs--dashboard)
- [Custom Actions](#custom-actions)
- [Building Executables](#building-executables)
- [Requirements & Dependencies](#requirements--dependencies)

---

## Quick Start

### Prerequisites

- **Python 3.11** (or compatible 3.x)
- **Chrome** (for UI tests; ChromeDriver is auto-downloaded to match your installed Chrome version via webdriver-manager)
- Optional: Excel support needs `openpyxl` (see [Requirements](#requirements--dependencies))

### Install and Run

```bash
# Clone or open the project, then:
pip install -r requirements.txt

# Run with default config (f/fStart.json → uses y/Mix.json by default)
python f/fEngine.py

# Run a specific config
python f/fEngine.py --config y/YPAD.json

# Enable debug (screenshots, page source, error artifacts in _debug folder)
python f/fEngine.py --debug
```

Results appear under **`z/`** in timestamped folders (e.g. `z/20250603_103556_Mix/`), including CSV results and an HTML dashboard.

---

## Project Structure

| Path | Purpose |
|------|--------|
| **`f/fEngine.py`** | Main entry: loads config, runs plans/actions, resolves variables, generates dashboard. |
| **`f/fStart.json`** | Global config: list of test-suite configs, thread_count, timeout, headless, debug. |
| **`requirements.txt`** | Python dependencies (pandas, requests, selenium, pyinstaller, openpyxl). |
| **`x/`** | **Actions & capabilities** (engine extension point). |
| **`x/xActions.py`** | All action handlers: UI (Selenium), Math, API, JSON, AI, File, Email, DB, Logic, Cloud, IoT, Time, Phone, and `runAction()` dispatcher. |
| **`x/xCapa.csv`** | Catalog of supported actions (Module, Action, Doc, Input, Output). |
| **`x/xCustom.py`** | User-defined actions (inherit `ActionHandler`, add methods with `x` prefix). |
| **`y/`** | **Test suites**. Each suite has a JSON + folder (e.g. `y/Mix.json` + `y/Mix/`). |
| **`y/<Suite>.json`** | Points to CSV paths for `yPlans`, `yActions`, `yDesigns` (e.g. `y/Mix/y1Plans.csv`). |
| **`y/<Suite>/y1Plans.csv`** | Test plans: PlanId, PlanName, DesignId, Run, Tags, Output. |
| **`y/<Suite>/y2Actions.csv`** | Steps per plan: PlanId, StepId, StepInfo, ActionType, ActionName, Input, Output, Expected, Critical. |
| **`y/<Suite>/y3Designs.csv`** | Variable values per DesignId (Type, DataName, D1, D2, …). |
| **`z/`** | **Outputs**: run folders, results CSV, dashboard HTML, optional _errors.csv, zLog.txt. |
| **`z/zDash_template.html`** | HTML template for the results dashboard (summary cards + results table). |
| **`.github/workflows/build-executables.yml`** | CI: build PyInstaller executables on push/PR/tag and optionally create a Release. |

---

## Configuration

### Main config: `f/fStart.json`

```json
{
  "configs": ["y/Mix.json"],
  "thread_count": 4,
  "timeout": 10,
  "headless": false,
  "debug": false
}
```

- **`configs`**: List of test-suite JSON files to run (each points to a set of y1Plans, y2Actions, y3Designs).
- **`thread_count`**: Max parallel YPAD suites when multiple entries are in `configs` (capped by `min(thread_count, number of configs)`). With `thread_count` 1, or a single config, suites run one after another. Plans within a suite still run sequentially. CLI output for each suite is printed in `configs` order after that suite finishes (buffered so parallel runs do not interleave).
- **`timeout`**: Default action timeout (seconds).
- **`headless`**: If true, browser runs headless (overridable by env; cloud detection can force headless).
- **`debug`**: If true, enables debug artifacts (screenshots, page source, error files in `_debug`).

Optional **`tags`** in `f/fStart.json`: run only plans whose `Tags` column matches (e.g. `["UI"]` or `["All"]`).

### Suite config: e.g. `y/Mix.json`

```json
{
  "input_files": {
    "yPlans": ["y/Mix/y1Plans.csv"],
    "yActions": ["y/Mix/y2Actions.csv"],
    "yDesigns": ["y/Mix/y3Designs.csv"]
  }
}
```

You can list multiple files per key (e.g. multiple CSVs); they are concatenated. Supported file types: **CSV**, **TXT**, **Excel (.xlsx/.xls)**, **JSON**.

---

## Test Data (Plans, Actions, Designs)

### y1Plans.csv

| Column    | Description |
|-----------|-------------|
| PlanId    | Unique plan id (e.g. PLoginTest, PMath_Addition). |
| PlanName  | Human-readable name. |
| DesignId  | One or more design ids separated by `;` (e.g. D1 or D1;D2). Variables from y3Designs are picked by this. |
| Run       | Y = run this plan, N = skip. |
| Tags      | Optional; used with `tags` in f/fStart.json to filter plans. |
| Output    | Optional description. |

### y2Actions.csv

| Column     | Description |
|------------|-------------|
| PlanId    | Must match a row in y1Plans. |
| StepId    | Step number (e.g. 1, 2, 3). |
| StepInfo  | Short description of the step. |
| ActionType| e.g. xUI, xMath, xAPI, xReuse, xCustom. |
| ActionName| e.g. xOpenBrowser, xAdd, xGet, PLoginTest (for xReuse). |
| Input     | Semicolon-separated parameters; can use variable names from y3Designs (e.g. email;email_xpath). |
| Output    | Optional expected/output variable name. |
| Expected  | Optional expected value for pass/fail. |
| Critical  | Y = stop plan if this step fails; N = continue. |

### y3Designs.csv

| Column   | Description |
|----------|-------------|
| Type     | Category (e.g. UI, Math, API). |
| DataName | Variable name used in Input/Expected (e.g. email, v1, base_url_ps). |
| D1, D2, …| Value for each DesignId (D1, D2, D3, …). |

At runtime, **Input** and **Expected** in actions are resolved by replacing **DataName** with the value in the column that matches the current **DesignId**. So one plan can run against multiple “designs” (e.g. environments or datasets) by listing multiple DesignIds in the plan’s **DesignId** field.

---

## Action Types & Capabilities

Actions are implemented in **`x/xActions.py`** and documented in **`x/xCapa.csv`**. Summary:

| Type    | Examples | Description |
|---------|----------|-------------|
| **xUI** | xOpenBrowser, xNavigate, xClick, xType, xGetText, xSelectDropdown, xWaitFor, xCloseBrowser, xDragAndDrop, xHandleAlert, … | Selenium-based browser automation. Selectors: `css=`, `xpath=`, or auto-detected. |
| **xReuse** | (ActionName = PlanId) | Reuse another plan’s steps (e.g. login flow). |
| **xMath** | xAdd, xMinus, xMultiply, xDiv, xCompare, xPower, xModulo, xRound | Numeric operations. |
| **xAPI** | xGet, xPost, xPut, xDelete, xGetAuthToken | HTTP calls. |
| **xJSON** | xExtractJson, xCompareJson, xValidateJson | JSON parsing/assertion. |
| **xAI** | xTextPrompt, xContextPrompt | AI service calls. |
| **xFile** | xFileCopy, xFileDelete, xFileExists | File operations. |
| **xEmail** | xEmailSend, xEmailRead | SMTP/IMAP. |
| **xDB** | xDBConnect, xDBQuery, xDBInsert | SQLite (and similar). |
| **xLogic** | xLogicIf, xLogicSwitch | Conditional logic. |
| **xCloud** | xCloudUpload, xCloudDownload, xCloudListFiles, xCloudDeleteFile | Cloud storage. |
| **xIoT** | xIoTControl, xIoTSensor | IoT device control/sensors. |
| **xTime** | xTimeWait, xTimeSchedule | Wait/schedule. |
| **xPhone** | xMakeCall, xSendSMS | tel:/sms: style actions. |
| **xCustom** | (see xCustom.py) | User-defined methods in `x/xCustom.py` (name with `x` prefix). |

**xReuse** is handled in the engine: it runs the referenced plan’s actions (same DesignId context). **xCustom** requires `x/xCustom.py` and a method name matching the action (e.g. ActionName `xExampleFunction` → method `xExampleFunction` in CustomActionHandler).

---

## Running Tests

```bash
# Default: f/fStart.json → configs listed there (e.g. y/Mix.json)
python f/fEngine.py

# Custom main config
python f/fEngine.py --config path/to/start.json

# Debug mode: extra artifacts (screenshots, page source) in results_dir/_debug
python f/fEngine.py --debug
```

- Only plans with **Run = Y** are executed.
- If **tags** are set in `f/fStart.json`, only plans whose **Tags** match are run (special value **All** = no tag filter).
- **DesignId**: each plan is run once per design id (e.g. D1;D2 → run twice, with variables from D1 then D2).
- **Critical** steps: if a step with Critical=Y fails, the rest of that plan/design is skipped.
- **ChromeDriver**: auto-resolved to match installed Chrome (webdriver-manager); killed at startup to avoid leftovers; headless/cloud detection can enable headless automatically.

Results are written under **`z/<timestamp>_<SuiteName>/`**:
- **`<SuiteName>_zResults.csv`** – full results (DesignId, PlanId, StepId, ActionType, ActionName, Input, Output, Expected, Result, TimeTaken, …).
- **`<SuiteName>_zDash.html`** – dashboard (summary + table).
- **`_errors.csv`** – optional; failed steps appended here.

---

## Outputs & Dashboard

- **Directory**: `z/YYYYMMDD_HHMMSS_<SuiteName>/`.
- **CSV**: One row per action run; columns include PlanId, DesignId, StepId, ActionType, ActionName, Input, Output, Expected, Result, TimeTaken, Critical.
- **Dashboard**: Generated from **`z/zDash_template.html`**; placeholders like `{YPAD_NAME}`, `{TOTAL_ACTIONS}`, `{PASSED_ACTIONS}`, etc. are replaced, and the results array is injected. Open the generated `*_zDash.html` in a browser to view summary cards and a filterable results table.
- **Screenshots**: On UI failure, a screenshot can be saved next to results (and in `_debug` if debug is on); dashboard may show a link to it.
- **zLog.txt**: Historical run log (if logging was configured to write there).

---

## Custom Actions

1. Open **`x/xCustom.py`**.
2. In **`CustomActionHandler`**, add a method whose name starts with **`x`** and takes **`(self, aIn)`** (e.g. `xMyCheck(self, aIn)`).
3. Parse **`aIn`** (e.g. semicolon-separated): `parts = self.validate_input(aIn).split(';')`.
4. Return a string result (used for pass/fail if Expected is set).
5. In **y2Actions.csv**, use **ActionType = xCustom**, **ActionName = xMyCheck**, **Input = arg1;arg2**.

The framework loads **xCustom** only if the file exists; otherwise **xCustom** actions are unavailable.

---

## Building Executables

- **Local**: See **BUILD.md** for PyInstaller one-file command (with `--add-data` for `x/`, `z/zDash_template.html`, etc.). Use `:` on Unix and `;` on Windows for path separators in `--add-data`.
- **CI**: **`.github/workflows/build-executables.yml`** runs on push/PR to `main` and on tags `v*`. It builds Windows/macOS/Linux executables and uploads artifacts; if the run is triggered by a tag (e.g. `v1.0.0`), it creates a GitHub Release and attaches the executables.

Place **`f/fStart.json`** and the **`y/`** folder (or the subset you need) next to the built executable so it can find configs and CSV/JSON data.

---

## Requirements & Dependencies

From **requirements.txt**:

- **pandas** – CSV/Excel/JSON loading, result tables.
- **requests** – API actions.
- **selenium** – UI (Chrome) actions.
- **pyinstaller** – for building executables.
- **openpyxl** – for Excel input files (.xlsx).

Optional at runtime:

- **schedule** – for xTimeSchedule (if used).
- **pyautogui** – for some UI/accessibility use cases (import is optional in code).

**Chrome** must be installed for UI tests; the framework uses Selenium with Chrome and automatically downloads the matching ChromeDriver for your Chrome version (no PATH setup required).

---

## Summary for New Users

1. **Install**: `pip install -r requirements.txt`
2. **Configure**: Edit **f/fStart.json** (and optionally **tags**); ensure at least one suite JSON under **y/** points to valid **y1Plans**, **y2Actions**, **y3Designs**.
3. **Data**: Set **Run = Y** for plans to run; use **y3Designs** to define variables (DataName) per **DesignId** and reference them in **Input**/**Expected** in **y2Actions**.
4. **Run**: `python f/fEngine.py` (or `--config` / `--debug` as needed).
5. **Results**: Open **`z/<timestamp>_<SuiteName>/<SuiteName>_zDash.html`** and **`*_zResults.csv`**.
6. **Custom steps**: Add methods in **x/xCustom.py** and call them with **ActionType = xCustom** in **y2Actions.csv**.

For building distributable executables and CI, follow **BUILD.md** and the GitHub Actions workflow.
