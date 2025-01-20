#!/usr/bin/env python3

import os
import re
import subprocess
import requests
import json
import argparse

DEFAULT_LINTER_CMD = "/Users/btofel/go/bin/golangci-lint-v1.61.0 run --enable-only nestif"
LLM_URL = "http://127.0.0.1:8080/v1/chat/completions"
MODEL_NAME = "ticlazau/granite-3.1-8b-instruct_Q8_0"

SYSTEM_PROMPT = (
    "You are a Go expert.\n"
    "Your job is to remove ALL nested if statements from the user-provided code snippet.\n"
    "Use early returns or separate logic so that no 'if' statements appear within another 'if' block.\n"
    "Do NOT introduce a new function or rename the existing function.\n"
    "Keep the same function signature, variable names, and overall structure.\n"
    "Return ONLY the rewritten snippet enclosed in triple backticks, with no extra commentary.\n"
)

TEMPERATURE = 0.2
TOP_P = 0.8
DEBUG_FILE = "pkg/sqlite/load.go"

def run_linter(linter_cmd, working_dir):
    try:
        result = subprocess.run(
            linter_cmd.split(),
            capture_output=True,
            text=True,
            check=False,
            cwd=working_dir
        )
        return result.stdout.splitlines()
    except Exception as e:
        print(f"[ERROR] Could not run linter: {e}")
        return []

def parse_nestif_errors(lint_output_lines):
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
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()

def extract_nested_if_snippet(file_contents, start_line):
    lines = file_contents.splitlines()
    start_index = start_line - 1
    if start_index < 0 or start_index >= len(lines):
        return ""
    snippet_lines = []
    brace_count = 0
    found_open_brace = False
    for i in range(start_index, len(lines)):
        line = lines[i]
        snippet_lines.append(line)
        open_count = line.count("{")
        close_count = line.count("}")
        brace_count += open_count
        brace_count -= close_count
        if open_count > 0:
            found_open_brace = True
        if found_open_brace and brace_count <= 0:
            break
    return "\n".join(snippet_lines)

def replace_snippet_in_file(original_code, snippet, new_snippet):
    return original_code.replace(snippet, new_snippet, 1)

def extract_code_from_response(llm_content):
    pattern = re.compile(r'```go\s*(.*?)\s*```', re.DOTALL)
    match = pattern.search(llm_content)
    if match:
        return match.group(1).strip()
    return llm_content.strip()

def call_llm_for_fix(snippet):
    user_msg = (
        f"Here is the code snippet:\n\n```go\n{snippet}\n```\n"
        "Please rewrite it to remove all nested if statements. Keep the existing function signature and variables. "
        "Return ONLY the updated snippet between triple backticks, with no extra commentary."
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
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
            timeout=120
        )
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"[ERROR] Request to LLM failed: {e}")
        return snippet
    response_json = response.json()
    if not response_json.get("choices"):
        print("[ERROR] No choices in response from LLM.")
        return snippet
    llm_content = response_json["choices"][0]["message"]["content"]
    return extract_code_from_response(llm_content)

def write_file_contents(filepath, contents):
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(contents)

def run_go_test_if_exists(filepath, working_dir):
    test_file = re.sub(r"\.go$", "_test.go", filepath)
    test_path = os.path.join(working_dir, test_file)
    if os.path.exists(test_path):
        result = subprocess.run(
            ["go", "test", test_file],
            capture_output=True,
            text=True,
            cwd=working_dir
        )
        if result.returncode == 0:
            print(result.stdout)
        else:
            if result.stdout:
                print(result.stdout)
            if result.stderr:
                print(result.stderr)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.getcwd())
    parser.add_argument("--linter-cmd", default=DEFAULT_LINTER_CMD)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    repo_dir = os.path.abspath(args.repo)
    linter_cmd = args.linter_cmd
    debug_mode = args.debug
    original_lint_output = run_linter(linter_cmd, repo_dir)
    nestif_errors = parse_nestif_errors(original_lint_output)
    if not nestif_errors:
        return
    files_to_fix = {err[0] for err in nestif_errors}
    if debug_mode:
        if DEBUG_FILE in files_to_fix:
            files_to_fix = {DEBUG_FILE}
        else:
            return
    for filename in files_to_fix:
        file_path = os.path.join(repo_dir, filename)
        if not os.path.exists(file_path):
            continue
        original_code = get_file_contents(file_path)
        error_lines = [line for (f, line, msg) in nestif_errors if f == filename]
        error_lines.sort(reverse=True)
        new_file_code = original_code
        for line_num in error_lines:
            snippet = extract_nested_if_snippet(new_file_code, line_num)
            if not snippet.strip():
                continue
            fixed_snippet = call_llm_for_fix(snippet)
            updated = replace_snippet_in_file(new_file_code, snippet, fixed_snippet)
            if updated != new_file_code or fixed_snippet == snippet:
                new_file_code = updated
        write_file_contents(file_path, new_file_code)
        recheck_output_lines = run_linter(linter_cmd, repo_dir)
        post_fix_errors = parse_nestif_errors(recheck_output_lines)
        still_has_error = any(ferr[0] == filename for ferr in post_fix_errors)
        if not still_has_error:
            run_go_test_if_exists(filename, repo_dir)

if __name__ == "__main__":
    main()