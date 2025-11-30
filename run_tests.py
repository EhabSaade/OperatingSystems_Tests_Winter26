#!/usr/bin/env python3
# Test runner for C++ smash (no simulator)
# Compares smash output to expected files in outputs/expected/
# Includes ANSI color output

import os
import sys
import subprocess
import signal
import time
import re
import pty
import shutil

# -----------------------------------------------------
# Colors
# -----------------------------------------------------
RED    = "\033[31m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
BLUE   = "\033[34m"
RESET  = "\033[0m"

TESTCASE_DIR = "inputs"
OUTPUT_DIR = "outputs"
CPP_OUT = os.path.join(OUTPUT_DIR, "output")
EXP_OUT = os.path.join(OUTPUT_DIR, "expected")

SMASH_BIN = "./smash"

PROMPT_CHARS = ["smash> ", "smash>"]
PID_RE = re.compile(r'\b\d{2,6}\b')


# -----------------------------------------------------
# Compile smash using your Makefile settings
# -----------------------------------------------------
def compile_smash():
    print(f"{YELLOW}Compiling smash...{RESET}\n")

    cmd = [
        "g++",
        "--std=c++11",
        "-Wall",
        "Commands.cpp",
        "signals.cpp",
        "smash.cpp",
        "-o",
        "smash"
    ]

    try:
        subprocess.check_call(cmd)
        print(f"{GREEN}Compilation OK.{RESET}\n")
    except subprocess.CalledProcessError:
        print(f"{RED}Compilation FAILED.{RESET}")
        sys.exit(1)


# -----------------------------------------------------
# DU controlled environment
# -----------------------------------------------------
def prepare_du_environment():
    """Create deterministic du test environment"""
    if os.path.exists("test_env_du"):
        shutil.rmtree("test_env_du")

    os.mkdir("test_env_du")

    # Files with exact known sizes
    with open("test_env_du/a", "wb") as f:
        f.write(b"A" * 500)      # 500 bytes

    with open("test_env_du/b", "wb") as f:
        f.write(b"B" * 2500)     # 2500 bytes

    os.mkdir("test_env_du/sub")

    with open("test_env_du/sub/c", "wb") as f:
        f.write(b"C" * 300)      # 300 bytes


def cleanup_du_environment():
    if os.path.exists("test_env_du"):
        shutil.rmtree("test_env_du")


# -----------------------------------------------------
# Directory setup
# -----------------------------------------------------
def ensure_dirs():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(CPP_OUT, exist_ok=True)
    os.makedirs(EXP_OUT, exist_ok=True)


def read_file(path):
    try:
        with open(path, "r") as f:
            return f.read().splitlines()
    except:
        return []


# -----------------------------------------------------
# PID Normalization
# -----------------------------------------------------
def normalize_line(line):
    l = line

    # Remove smash> prompt
    for p in PROMPT_CHARS:
        if l.startswith(p):
            l = l[len(p):]

    # CASE 1 — last token is PID
    tokens = l.split()
    if len(tokens) >= 2:
        last = tokens[-1]
        if PID_RE.fullmatch(last):
            tokens[-1] = "<PID>"
            return " ".join(tokens)

    # CASE 2 — "PID: text"
    parts = l.split(": ", 1)
    if len(parts) == 2 and PID_RE.fullmatch(parts[0]):
        return "<PID>: " + parts[1]

    # CASE 3 — smash SIGINT messages
    if l.startswith("smash: process "):
        t = l.split()
        if len(t) >= 4 and PID_RE.fullmatch(t[2]):
            t[2] = "<PID>"
            return " ".join(t)

    return l


def normalize_output(lines):
    return [normalize_line(l) for l in lines]


# -----------------------------------------------------
# Normal test runner
# -----------------------------------------------------
def run_cpp_smash(testfile, outfile):
    env = os.environ.copy()
    env["VAR1"] = "HELLO"
    env["VAR2"] = "EHAB"

    with open(testfile, "r") as inp:
        proc = subprocess.Popen(
            SMASH_BIN,
            stdin=inp,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            universal_newlines=True
        )
        out, _ = proc.communicate()

    with open(outfile, "w") as f:
        f.write(out)


# -----------------------------------------------------
# Ctrl-C test runner (using PTY)
# -----------------------------------------------------
def run_ctrlc(testfile, outfile):
    env = os.environ.copy()
    env["VAR1"] = "HELLO"
    env["VAR2"] = "EHAB"

    master, slave = pty.openpty()

    proc = subprocess.Popen(
        SMASH_BIN,
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        universal_newlines=True,
        preexec_fn=os.setsid      # own process group
    )

    # Send test lines
    with open(testfile, "r") as inp:
        for line in inp.read().splitlines():
            os.write(master, (line + "\n").encode())
            time.sleep(0.1)

    time.sleep(0.4)

    # Send SIGINT to smash group
    os.killpg(proc.pid, signal.SIGINT)
    time.sleep(0.4)

    # Terminate cleanly
    os.write(master, b"quit\n")

    out, _ = proc.communicate()
    out = out.replace("\r", "")

    with open(outfile, "w") as f:
        f.write(out)


# -----------------------------------------------------
# Output comparison
# -----------------------------------------------------
def compare_outputs(cpp_file, expected_file):
    cpp_lines = normalize_output(read_file(cpp_file))
    exp_lines = normalize_output(read_file(expected_file))

    diffs = []
    max_len = max(len(cpp_lines), len(exp_lines))

    for i in range(max_len):
        c = cpp_lines[i] if i < len(cpp_lines) else ""
        e = exp_lines[i] if i < len(exp_lines) else ""
        if c != e:
            diffs.append((i + 1, c, e))

    return diffs


# -----------------------------------------------------
# Test executor
# -----------------------------------------------------
def run_test(testname):
    print(f"\n{BLUE}Running test: {testname}{RESET}")

    du_mode = ("du" in testname.lower())
    if du_mode:
        prepare_du_environment()

    testfile = os.path.join(TESTCASE_DIR, testname)
    cpp_file = os.path.join(CPP_OUT, testname + ".out")
    exp_file = os.path.join(EXP_OUT, testname + ".out")

    if not os.path.exists(testfile):
        print(f"{RED}Test '{testname}' not found.{RESET}")
        return

    if testname.lower().startswith("ctrlc"):
        run_ctrlc(testfile, cpp_file)
    else:
        run_cpp_smash(testfile, cpp_file)

    if du_mode:
        cleanup_du_environment()

    diffs = compare_outputs(cpp_file, exp_file)

    if not diffs:
        print(f"{GREEN}PASS{RESET}: {testname}")
    else:
        print(f"{RED}FAIL{RESET}: {testname}")
        for lineno, c, e in diffs:
            print(f"{YELLOW}Line {lineno}:{RESET}")
            print(f"  {RED}your:     '{c}'{RESET}")
            print(f"  {GREEN}expected: '{e}'{RESET}")


# -----------------------------------------------------
# Main
# -----------------------------------------------------
def main():
    compile_smash()
    ensure_dirs()

    # Run specific test
    if len(sys.argv) == 2:
        run_test(sys.argv[1])
        return

    # Run all tests
    tests = sorted(os.listdir(TESTCASE_DIR))
    for t in tests:
        if t.endswith(".txt"):
            run_test(t)


if __name__ == "__main__":
    main()
