#!/usr/bin/env python3

import os
import re
import subprocess
import requests
import json

# -------------------------------------
# Configuration
# -------------------------------------
LINTER_CMD = "/Users/btofel/go/bin/golangci-lint-v1.61.0 run"
LLM_URL = "http://127.0.0.1:8080/v1/chat/completions"
MODEL_NAME = "granite-3.0-8b-instruct"  # or whatever model you need
SYSTEM_PROMPT = "You are ChatGPT, a large language model trained by OpenAI.\n" \
                "Please fix the following Go code to remove nested if statements while preserving functionality."
TEMPERATURE = 0.2  # LLM parameter
TOP_P = 1.0        # LLM parameter
# -------------------------------------

def run_linter():
    """
    Runs golangci-lint and returns its raw stdout as a list of lines.
    """
    print("[INFO] Running golangci-lint...")
    try:
        result = subprocess.run(
            LINTER_CMD.split(),
            capture_output=True,
            text=True,
            check=False  # Don't raise an error if lint fails
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
            nestif_errors.append(
                (
                    match.group("file"),
                    int(match.group("line")),
                    match.group("message")
                )
            )
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
    # Prepare the request payload
    # Equivalent to:
    # curl --location 'http://127.0.0.1:8080/v1/chat/completions' \
    #   --header 'Content-Type: application/json' \
    #   --data '{"model": ... }'
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

    # Parse out the 'content' from the LLM response
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

def run_go_test_if_exists(filepath):
    """
    If there's a test file (e.g., 'foo_test.go'), run 'go test' on that file.
    """
    # We assume the test file would be: <filename without .go>_test.go
    # Example: If filepath is pkg/sqlite/load.go -> pkg/sqlite/load_test.go
    test_file = re.sub(r'\.go$', '_test.go', filepath)
    if os.path.exists(test_file):
        print(f"[INFO] Found test file: {test_file}, running `go test`...")
        try:
            # We only test that one file specifically:
            subprocess.run(
                ["go", "test", test_file],
                check=True
            )
            print("[INFO] Test passed.")
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] Test failed: {e}")
    else:
        print("[INFO] No test file found for:", filepath)

def main():
    """
    Main flow:
      1. Run golangci-lint.
      2. Find all nestif errors.
      3. For each nestif error, fix the code with the LLM and overwrite.
      4. Re-run linter to confirm fix.
      5. If no nestif error remains, optionally run go test.
    """
    original_lint_output = run_linter()
    nestif_errors = parse_nestif_errors(original_lint_output)

    if not nestif_errors:
        print("[INFO] No 'nestif' errors found. Exiting.")
        return

    # You may have multiple nestif errors in the same file
    # We'll group them by filename to fix once per file
    files_to_fix = {err[0] for err in nestif_errors}

    for filename in files_to_fix:
        print(f"[INFO] Attempting to fix nestif errors in file: {filename}")

        # 1. Read file contents
        original_code = get_file_contents(filename)

        # 2. Send to LLM for fix
        fixed_code = fix_code_with_llm(original_code)

        # 3. Overwrite file with the fixed code
        write_file_contents(filename, fixed_code)

        # 4. Re-run the linter specifically on that file (optimization)
        #    Or you can re-run on the entire repo for a thorough check
        recheck_cmd = f"{LINTER_CMD} {filename}"
        result = subprocess.run(
            recheck_cmd.split(),
            capture_output=True,
            text=True,
            check=False
        )
        recheck_output_lines = result.stdout.splitlines()
        # Check if there is still a nestif error for this file
        recheck_nestif_errors = [
            l for l in recheck_output_lines if "nestif" in l and filename in l
        ]
        if recheck_nestif_errors:
            print("[WARNING] 'nestif' issue still present after fix attempt:")
            for e in recheck_nestif_errors:
                print("  ", e)
        else:
            print(f"[INFO] 'nestif' errors resolved in {filename}.")
            # 5. Optionally run test if <filename>_test.go exists
            run_go_test_if_exists(filename)


if __name__ == "__main__":
    main()