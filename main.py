#!/usr/bin/env python3

import os
import re
import subprocess
import requests
import json
import argparse

# -------------------------------------
# Configuration Defaults
# -------------------------------------
DEFAULT_LINTER_CMD = "/Users/btofel/go/bin/golangci-lint-v1.61.0 run"
LLM_URL = "http://127.0.0.1:8080/v1/chat/completions"
MODEL_NAME = "granite-3.0-8b-instruct"  # Or whichever model you're using
SYSTEM_PROMPT = (
    "You are ChatGPT, a large language model trained by OpenAI.\n"
    "Please fix the following Go code to remove nested if statements while preserving functionality."
)
TEMPERATURE = 0.2
TOP_P = 1.0

DEBUG_FILE = "pkg/sqlite/load.go"
# -------------------------------------

def run_linter(linter_cmd, working_dir):
    """
    Runs golangci-lint from the specified working directory and returns its raw stdout as a list of lines.
    """
    print(f"[INFO] Running golangci-lint in '{working_dir}'...")
    try:
        result = subprocess.run(
            linter_cmd.split(),
            capture_output=True,
            text=True,
            check=False,  # Don't raise an error if lint fails
            cwd=working_dir
        )
        # Return each line from stdout
        return result.stdout.splitlines()
    except Exception as e:
        print(f"[ERROR] Could not run linter: {e}")
        return []

def parse_nestif_errors(lint_output_lines):
    """
    From the lint output, find lines referencing 'nestif' errors.
    Returns a list of tuples: [(filename, line_number, error_msg), ...]
    """
    nestif_regex = re.compile(r'^(?P<file>.+?):(?P<line>\d+):\d+\s+nestif\s+(?P<message>.+)$')
    nestif_errors = []
    for line in lint_output_lines:
        match = nestif_regex.match(line)
        if match:
            filename = match.group("file")
            line_num = int(match.group("line"))
            message = match.group("message")
            nestif_errors.append((filename, line_num, message))
    return nestif_errors

def get_file_contents(filepath):
    """
    Read the entire content of a given file.
    """
    with open(filepath, 'r', encoding='utf-8') as f:
        return f.read()

def fix_code_with_llm(file_contents):
    """
    Sends the file contents to the LLM to be fixed, returns the updated code.
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Please remove nested if statements from this Go code without changing its functionality.\n\n"
                f"```go\n{file_contents}\n```"
            ),
        },
    ]

    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": TEMPERATURE,
        "top_p": TOP_P
    }

    try:
        response = requests.post(
            LLM_URL,
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=60
        )
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"[ERROR] Request to LLM failed: {e}")
        return file_contents  # fallback, no changes

    response_json = response.json()
    if not response_json.get("choices"):
        print("[ERROR] No choices in response from LLM.")
        return file_contents

    llm_content = response_json["choices"][0]["message"]["content"]
    return llm_content

def write_file_contents(filepath, contents):
    """
    Overwrite the file with new contents.
    """
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(contents)

def run_go_test_if_exists(filepath, working_dir):
    """
    If there's a test file (e.g., 'foo_test.go'), run 'go test' on that file in the given working_dir.
    """
    test_file = re.sub(r'\.go$', '_test.go', filepath)
    test_path = os.path.join(working_dir, test_file)
    if os.path.exists(test_path):
        print(f"[INFO] Found test file: {test_file}, running `go test` in '{working_dir}'...")
        try:
            subprocess.run(
                ["go", "test", test_file],
                check=True,
                cwd=working_dir
            )
            print("[INFO] Test passed.")
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] Test failed: {e}")
    else:
        print("[INFO] No test file found for:", filepath)

def main():
    # -------------------------------------
    # Parse Command-Line Args
    # -------------------------------------
    parser = argparse.ArgumentParser(
        description="Run golangci-lint to find nestif errors, fix them via LLM, and re-run linter."
    )
    parser.add_argument(
        "--repo",
        default=os.getcwd(),
        help="Path to the Go project directory (defaults to current directory)."
    )
    parser.add_argument(
        "--linter-cmd",
        default=DEFAULT_LINTER_CMD,
        help="Command to run golangci-lint (defaults to a predefined path)."
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=f"Operate only on '{DEBUG_FILE}' for faster debugging."
    )

    args = parser.parse_args()
    repo_dir = os.path.abspath(args.repo)
    linter_cmd = args.linter_cmd
    debug_mode = args.debug

    print(f"[INFO] Using repo directory: {repo_dir}")
    print(f"[INFO] Using linter command: {linter_cmd}")
    if debug_mode:
        print("[DEBUG] Debug mode enabled. Will only fix nestif errors in:", DEBUG_FILE)

    # 1. Initial linter run
    original_lint_output = run_linter(linter_cmd, repo_dir)
    # 2. Parse nestif errors
    nestif_errors = parse_nestif_errors(original_lint_output)

    # Show found errors in the console
    if not nestif_errors:
        print("[INFO] No 'nestif' errors found. Exiting.")
        return
    else:
        print("[INFO] Found 'nestif' errors:")
        for (f, line, msg) in nestif_errors:
            print(f"   {f}:{line} -> {msg}")

    # 3. We may have multiple nestif errors in the same file -> fix once per file
    files_to_fix = {err[0] for err in nestif_errors}

    # If we are in debug mode, only fix the debug file (if it had errors).
    if debug_mode:
        if DEBUG_FILE in files_to_fix:
            files_to_fix = {DEBUG_FILE}
        else:
            print(f"[DEBUG] '{DEBUG_FILE}' had no nestif errors or wasn't in the list.")
            return

    # Show the list of files to be fixed
    print("[INFO] The following files will be processed:")
    for f in files_to_fix:
        print("   ", f)

    for filename in files_to_fix:
        print(f"[INFO] Attempting to fix nestif errors in file: {filename}")

        # Build the absolute path to the file
        file_path = os.path.join(repo_dir, filename)
        if not os.path.exists(file_path):
            print(f"[ERROR] File not found: {file_path}. Skipping.")
            continue

        original_code = get_file_contents(file_path)

        # 4. Send to LLM for fix
        fixed_code = fix_code_with_llm(original_code)

        # 5. Overwrite file with the fixed code
        write_file_contents(file_path, fixed_code)

        # 6. Re-run the linter in full (not just the file)
        print(f"[INFO] Re-checking lint for the entire repo: {repo_dir}")
        recheck_output_lines = run_linter(linter_cmd, repo_dir)

        # Parse nestif errors again
        post_fix_errors = parse_nestif_errors(recheck_output_lines)

        # If we still see the same file in nestif errors, then fix didn't remove it
        still_has_error = any(ferr[0] == filename for ferr in post_fix_errors)
        if still_has_error:
            print("[WARNING] 'nestif' issue still present after fix attempt:")
            for e in post_fix_errors:
                if e[0] == filename:
                    print("  ", e)
        else:
            print(f"[INFO] 'nestif' errors resolved in {filename}.")
            # 7. Optionally run test if <filename>_test.go exists
            run_go_test_if_exists(filename, repo_dir)


if __name__ == "__main__":
    main()