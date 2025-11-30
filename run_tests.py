#!/usr/bin/env python3
# Test runner for C++ smash vs embedded Python simulator
# Compares smash output to simulator output (no static expected files)
# Includes ANSI color output

import os
import sys
import subprocess
import signal
import time
import re
import pty
import shutil
import shlex
import pwd
import socket
import platform
import datetime

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
SIM_OUT = os.path.join(OUTPUT_DIR, "expected")

SMASH_BIN = "./smash"
# When we run as simulator, we call this same file with --sim
SIM_BIN = [sys.executable, os.path.abspath(__file__), "--sim"]

PROMPT_CHARS = ["smash> ", "smash>"]
PID_RE = re.compile(r'\b\d{2,6}\b')
PROMPT_DEFAULT = "smash"


# =====================================================
#  C++ COMPILATION
# =====================================================

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


# =====================================================
#  DU CONTROLLED ENVIRONMENT (optional)
# =====================================================

def prepare_du_environment():
    """Create deterministic du test environment"""
    if os.path.exists("test_env_du"):
        shutil.rmtree("test_env_du")

    os.mkdir("test_env_du")

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


# =====================================================
#  FILE / NORMALIZATION HELPERS
# =====================================================

def ensure_dirs():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(CPP_OUT, exist_ok=True)
    os.makedirs(SIM_OUT, exist_ok=True)


def read_file(path):
    try:
        with open(path, "r") as f:
            return f.read().splitlines()
    except:
        return []


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


# =====================================================
#  RUNNING SMASH / SIMULATOR (NORMAL TESTS)
# =====================================================

def run_proc_with_input(binary, testfile, outfile):
    env = os.environ.copy()
    env["VAR1"] = "HELLO"
    env["VAR2"] = "EHAB"

    with open(testfile, "r") as inp:
        proc = subprocess.Popen(
            binary,
            stdin=inp,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            universal_newlines=True
        )
        out, _ = proc.communicate()

    with open(outfile, "w") as f:
        f.write(out)


def run_cpp_smash(testfile, outfile):
    run_proc_with_input(SMASH_BIN, testfile, outfile)


def run_python_sim(testfile, outfile):
    run_proc_with_input(SIM_BIN, testfile, outfile)


# =====================================================
#  CTRL-C TESTS (PTY + SIGINT) FOR BOTH
# =====================================================

def run_ctrlc_generic(binary, testfile, outfile):
    env = os.environ.copy()
    env["VAR1"] = "HELLO"
    env["VAR2"] = "EHAB"

    master, slave = pty.openpty()

    proc = subprocess.Popen(
        binary,
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        universal_newlines=True,
        preexec_fn=os.setsid  # own process group
    )

    # send commands from the test file
    with open(testfile, "r") as inp:
        for line in inp.read().splitlines():
            os.write(master, (line + "\n").encode())
            time.sleep(0.1)

    time.sleep(0.4)

    # send Ctrl-C (SIGINT) to the group
    os.killpg(proc.pid, signal.SIGINT)
    time.sleep(0.4)

    # send quit to exit cleanly
    os.write(master, b"quit\n")

    out, _ = proc.communicate()
    out = out.replace("\r", "")

    with open(outfile, "w") as f:
        f.write(out)


def run_cpp_ctrlc(testfile, outfile):
    run_ctrlc_generic(SMASH_BIN, testfile, outfile)


def run_sim_ctrlc(testfile, outfile):
    run_ctrlc_generic(SIM_BIN, testfile, outfile)


# =====================================================
#  COMPARISON
# =====================================================

def compare_outputs(cpp_file, sim_file):
    cpp_lines = normalize_output(read_file(cpp_file))
    sim_lines = normalize_output(read_file(sim_file))

    diffs = []
    max_len = max(len(cpp_lines), len(sim_lines))

    for i in range(max_len):
        c = cpp_lines[i] if i < len(cpp_lines) else ""
        s = sim_lines[i] if i < len(sim_lines) else ""
        if c != s:
            diffs.append((i + 1, c, s))

    return diffs


# =====================================================
#  TEST EXECUTOR
# =====================================================

def run_test(testname):
    print(f"\n{BLUE}Running test: {testname}{RESET}")

    du_mode = ("du" in testname.lower())
    if du_mode:
        prepare_du_environment()

    testfile = os.path.join(TESTCASE_DIR, testname)
    cpp_file = os.path.join(CPP_OUT, testname + ".out")
    sim_file = os.path.join(SIM_OUT, testname + ".out")

    if not os.path.exists(testfile):
        print(f"{RED}Test '{testname}' not found.{RESET}")
        if du_mode:
            cleanup_du_environment()
        return

    # Ctrl-C tests
    if testname.lower().startswith("ctrlc"):
        run_cpp_ctrlc(testfile, cpp_file)
        run_sim_ctrlc(testfile, sim_file)
    else:
        run_cpp_smash(testfile, cpp_file)
        run_python_sim(testfile, sim_file)

    if du_mode:
        cleanup_du_environment()

    diffs = compare_outputs(cpp_file, sim_file)

    if not diffs:
        print(f"{GREEN}PASS{RESET}: {testname}")
    else:
        print(f"{RED}FAIL{RESET}: {testname}")
        for lineno, c, s in diffs:
            print(f"{YELLOW}Line {lineno}:{RESET}")
            print(f"  {RED}your:     '{c}'{RESET}")
            print(f"  {GREEN}expected: '{s}'{RESET}")


def run_all_tests():
    compile_smash()
    ensure_dirs()

    # Run specific test
    if len(sys.argv) == 2:
        # If only one arg and it's not --sim, it's a test name
        if sys.argv[1] != "--sim":
            run_test(sys.argv[1])
            return

    # Run all tests
    tests = sorted(os.listdir(TESTCASE_DIR))
    for t in tests:
        if t.endswith(".txt"):
            run_test(t)


# =====================================================
#  EMBEDDED SIMULATOR (REFERENCE IMPLEMENTATION)
# =====================================================

class Job:
    def __init__(self, job_id, pid, cmd_line, start_time, is_stopped=False, pgid=None):
        self.job_id = job_id
        self.pid = pid
        self.cmd_line = cmd_line  # exact original line (including &)
        self.start_time = start_time
        self.is_stopped = is_stopped
        self.pgid = pgid


class SmashShell:
    def __init__(self):
        self.prompt = PROMPT_DEFAULT
        self.jobs = []
        self.next_job_id = 1
        self.fg_job = None
        self.aliases = {}
        self.history = []
        self.oldpwd = None

        signal.signal(signal.SIGINT, self.handle_sigint)

    # --------------------------------------------------------
    # UTILITIES
    # --------------------------------------------------------

    def print_error(self, msg):
        print(msg, file=sys.stderr)

    def cleanup_jobs(self):
        alive = []
        for job in self.jobs:
            try:
                pid, _ = os.waitpid(job.pid, os.WNOHANG)
                if pid == 0:
                    alive.append(job)
            except:
                pass
        self.jobs = alive

    def add_job(self, pid, cmd_line, pgid=None, stopped=False):
        job = Job(self.next_job_id, pid, cmd_line, time.time(), stopped, pgid)
        self.jobs.append(job)
        self.next_job_id += 1
        return job

    def find_job_by_id(self, job_id):
        for job in self.jobs:
            if job.job_id == job_id:
                return job
        return None

    def update_job_finished(self, pid):
        self.jobs = [j for j in self.jobs if j.pid != pid]

    # --------------------------------------------------------
    # SIGNAL HANDLING
    # --------------------------------------------------------

    def handle_sigint(self, signum, frame):
        if self.fg_job is None:
            return

        print("smash: got ctrl-C")
        try:
            os.killpg(self.fg_job.pgid, signal.SIGKILL)
        except:
            pass

        print(f"smash: process {self.fg_job.pid} was killed")
        self.fg_job = None

    # --------------------------------------------------------
       # BUILT-IN COMMANDS
    # --------------------------------------------------------

    def cmd_chprompt(self, args):
        self.prompt = PROMPT_DEFAULT if not args else args[0]

    def cmd_showpid(self, args):
        if args:
            self.print_error("smash error: showpid: invalid arguments")
            return
        print(f"smash pid is {os.getpid()}")

    def cmd_pwd(self, args):
        if args:
            self.print_error("smash error: pwd: invalid arguments")
            return
        try:
            print(os.getcwd())
        except:
            self.print_error("smash error: getcwd failed")

    def cmd_cd(self, args):
        if len(args) == 0:
            return
        if len(args) > 1:
            self.print_error("smash error: cd: too many arguments")
            return

        target = args[0]
        if target == "-":
            if self.oldpwd is None:
                self.print_error("smash error: cd: OLDPWD not set")
                return
            target = self.oldpwd

        try:
            cwd = os.getcwd()
            os.chdir(target)
            self.oldpwd = cwd
        except:
            self.print_error("smash error: chdir failed")

    def cmd_jobs(self, args):
        if args:
            self.print_error("smash error: jobs: invalid arguments")
            return
        self.cleanup_jobs()

        for job in sorted(self.jobs, key=lambda j: j.job_id):
            print(f"[{job.job_id}] {job.cmd_line}")  # exact original, with &

    def cmd_fg(self, args):
        self.cleanup_jobs()

        if len(args) > 1:
            self.print_error("smash error: fg: invalid arguments")
            return

        if len(args) == 1:
            # error priority: invalid argument BEFORE checking jobs list
            try:
                jid = int(args[0])
            except:
                self.print_error("smash error: fg: invalid arguments")
                return

            if not self.jobs:
                self.print_error("smash error: fg: jobs list is empty")
                return

            job = self.find_job_by_id(jid)
            if job is None:
                self.print_error(f"smash error: fg: job-id {jid} does not exist")
                return
        else:
            if not self.jobs:
                self.print_error("smash error: fg: jobs list is empty")
                return
            job = sorted(self.jobs, key=lambda j: j.job_id)[-1]

        print(f"{job.cmd_line} {job.pid}")

        self.fg_job = job
        try:
            os.killpg(job.pgid, signal.SIGCONT)
        except:
            pass

        try:
            os.waitpid(job.pid, 0)
        except:
            pass

        self.update_job_finished(job.pid)
        self.fg_job = None

    def cmd_quit(self, args):
        if len(args) > 1 or (len(args) == 1 and args[0] != "kill"):
            self.print_error("smash error: quit: invalid arguments")
            return

        kill_flag = (args == ["kill"])

        if kill_flag:
            self.cleanup_jobs()
            print(f"smash: sending SIGKILL signal to {len(self.jobs)} jobs:")
            for job in self.jobs:
                print(f"{job.pid}: {job.cmd_line}")
                try:
                    os.killpg(job.pgid, signal.SIGKILL)
                except:
                    pass
                try:
                    os.waitpid(job.pid, 0)
                except:
                    pass

        raise SystemExit(0)

    def cmd_alias(self, args):
        if not args:
            for k, v in self.aliases.items():
                print(f"{k}='{v}'")
            return

        try:
            entry = " ".join(args)
            name, val = entry.split("=", 1)
        except:
            self.print_error("smash error: alias: invalid arguments")
            return

        name = name.strip()
        val = val.strip()

        if (val.startswith("'") and val.endswith("'")) or \
           (val.startswith('"') and val.endswith('"')):
            val = val[1:-1]

        self.aliases[name] = val

    def cmd_unalias(self, args):
        if len(args) != 1:
            self.print_error("smash error: unalias: invalid arguments")
            return
        if args[0] not in self.aliases:
            self.print_error(f"smash error: unalias: {args[0]} alias does not exist")
            return
        del self.aliases[args[0]]

    def cmd_du(self, args):
        if len(args) > 1:
            self.print_error("smash error: du: too many arguments")
            return

        path = args[0] if args else "."
        total_bytes = 0

        for root, dirs, files in os.walk(path, topdown=True, followlinks=False):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    st = os.lstat(fp)
                    total_bytes += st.st_size
                except:
                    pass

        kb = (total_bytes + 1023) // 1024
        print(f"Total disk usage: {kb} KB")

    def cmd_whoami(self, args):
        uid = os.getuid()
        gid = os.getgid()
        try:
            pw = pwd.getpwuid(uid)
            uname = pw.pw_name
            home = pw.pw_dir
        except:
            uname, home = "unknown", "N/A"

        print(uname)
        print(uid)
        print(gid)
        print(home)

    def cmd_unsetenv(self, args):
        if not args:
            self.print_error("smash error: unsetenv: not enough arguments")
            return

        for name in args:
            if name not in os.environ:
                self.print_error(f"smash error: unsetenv: {name} does not exist")
                return
            del os.environ[name]

    def cmd_sysinfo(self, args):
        system = platform.system()
        hostname = socket.gethostname()
        kernel = platform.release()
        arch = platform.machine()

        btime = "N/A"
        try:
            with open("/proc/stat") as f:
                for line in f:
                    if line.startswith("btime "):
                        ts = int(line.split()[1])
                        btime_dt = datetime.datetime.fromtimestamp(ts)
                        btime = btime_dt.strftime("%Y-%m-%d %H:%M:%S")
                        break
        except:
            pass

        print(f"System: {system}")
        print(f"Hostname: {hostname}")
        print(f"Kernel: {kernel}")
        print(f"Architecture: {arch}")
        print(f"Boot Time: {btime}")

    def cmd_usbinfo(self, args):
        root = "/sys/bus/usb/devices"
        if not os.path.isdir(root):
            self.print_error("smash error: usbinfo: no USB devices found")
            return

        def rfile(path):
            try:
                with open(path) as f:
                    return f.read().strip()
            except:
                return None

        devices = []

        for d in os.listdir(root):
            base = os.path.join(root, d)
            if not os.path.isdir(base):
                continue

            devnum = rfile(os.path.join(base, "devnum"))
            vendor = rfile(os.path.join(base, "idVendor"))
            product = rfile(os.path.join(base, "idProduct"))

            if not devnum or not vendor or not product:
                continue

            try:
                devnum_int = int(devnum)
            except:
                continue

            manu = rfile(os.path.join(base, "manufacturer")) or "N/A"
            prod = rfile(os.path.join(base, "product")) or "N/A"
            maxp = rfile(os.path.join(base, "bMaxPower")) or "N/A"
            maxp = maxp.replace(" ", "")

            devices.append((devnum_int, vendor, product, manu, prod, maxp))

        if not devices:
            self.print_error("smash error: usbinfo: no USB devices found")
            return

        devices.sort(key=lambda x: x[0])

        idx = 1
        for d in devices:
            dn, v, p, m, pr, mp = d
            print(f"Device {idx}: ID {v}:{p} {m} {pr} MaxPower: {mp}")
            idx += 1

    # --------------------------------------------------------
    # ALIAS EXPANSION – FIRST TOKEN ONLY
    # --------------------------------------------------------

    def expand_alias(self, line):
        try:
            tokens = shlex.split(line)
        except:
            return line

        if not tokens:
            return line

        first = tokens[0]

        if first in self.aliases:
            expanded = self.aliases[first]
            tokens = shlex.split(expanded) + tokens[1:]
            return " ".join(tokens)

        return line

    # --------------------------------------------------------
    # REDIRECTION PARSING (shared by builtins & external)
    # --------------------------------------------------------

    def parse_redirection(self, tokens):
        """
        Returns (cleaned_tokens, out_file, append, ok)
        Enforces:
        - at most one '>' or '>>'
        - exactly one filename after it
        - 'echo hi >' -> No such file or directory
        - 'echo hi > f g' -> invalid arguments
        """
        redir_indices = [i for i, t in enumerate(tokens) if t in (">", ">>")]
        if not redir_indices:
            return tokens, None, False, True

        if len(redir_indices) > 1:
            self.print_error("smash error: open failed: invalid arguments")
            return None, None, False, False

        i = redir_indices[0]

        # missing filename
        if i == len(tokens) - 1:
            self.print_error("smash error: open failed: No such file or directory")
            return None, None, False, False

        # must be exactly one filename after >
        if i != len(tokens) - 2:
            self.print_error("smash error: open failed: invalid arguments")
            return None, None, False, False

        out_file = tokens[i + 1]
        append = (tokens[i] == ">>")
        cleaned = tokens[:i]
        return cleaned, out_file, append, True

    # --------------------------------------------------------
    # RUN A BUILTIN BY NAME
    # --------------------------------------------------------

    def run_builtin(self, cmd, args):
        if cmd == "chprompt":   self.cmd_chprompt(args)
        elif cmd == "showpid":  self.cmd_showpid(args)
        elif cmd == "pwd":      self.cmd_pwd(args)
        elif cmd == "cd":       self.cmd_cd(args)
        elif cmd == "jobs":     self.cmd_jobs(args)
        elif cmd == "fg":       self.cmd_fg(args)
        elif cmd == "quit":     self.cmd_quit(args)
        elif cmd == "alias":    self.cmd_alias(args)
        elif cmd == "unalias":  self.cmd_unalias(args)
        elif cmd == "du":       self.cmd_du(args)
        elif cmd == "whoami":   self.cmd_whoami(args)
        elif cmd == "unsetenv": self.cmd_unsetenv(args)
        elif cmd == "sysinfo":  self.cmd_sysinfo(args)
        elif cmd == "usbinfo":  self.cmd_usbinfo(args)

    # --------------------------------------------------------
    # EXTERNAL COMMAND EXECUTION
    # --------------------------------------------------------

    def run_external(self, line, background, original_line):
        special_chars = ["|", "|&", ">", ">>", "*", "?"]

        if any(c in line for c in special_chars):
            cmd = ["bash", "-c", line]
        else:
            try:
                cmd = shlex.split(line)
            except:
                return

        try:
            p = subprocess.Popen(cmd, preexec_fn=os.setpgrp)
        except:
            self.print_error("smash error: execvp failed: No such file or directory")
            return

        pgid = p.pid

        if background:
            self.add_job(p.pid, original_line, pgid)
            return

        self.fg_job = Job(-1, p.pid, line, time.time(), False, pgid)
        try:
            p.wait()
        finally:
            self.fg_job = None

    # --------------------------------------------------------
    # COMMAND DISPATCH
    # --------------------------------------------------------

    def execute_line(self, line):
        line = line.strip()
        if not line:
            return

        original_line = line  # preserve exact input (with &)
        self.cleanup_jobs()
        self.history.append(line)

        # Alias expansion (first token only)
        line = self.expand_alias(line)

        # Background detection after alias expansion
        background = False
        if line.endswith("&"):
            line = line[:-1].strip()
            background = True

        # Tokenize once
        try:
            tokens = shlex.split(line)
        except:
            return

        if not tokens:
            return

        # Pipe detection: any '|' or '|&' makes it a complex external
        has_pipe = any(t in ("|", "|&") for t in tokens)

        if has_pipe:
            # Validate redirection semantics before passing to bash
            cleaned, out_file, append, ok = self.parse_redirection(tokens)
            if not ok:
                return
            # Let bash handle actual redirection and pipeline
            self.run_external(line, background, original_line)
            return

        # No pipes: handle redirection + builtin/external
        cleaned, out_file, append, ok = self.parse_redirection(tokens)
        if not ok:
            return
        if not cleaned:
            return

        cmd = cleaned[0]
        args = cleaned[1:]

        builtin_cmds = {
            "chprompt", "showpid", "pwd", "cd", "jobs", "fg", "quit",
            "alias", "unalias", "du", "whoami", "unsetenv", "sysinfo",
            "usbinfo"
        }

        if cmd in builtin_cmds:
            if out_file is not None:
                try:
                    saved_stdout = os.dup(1)
                except:
                    self.print_error("smash error: dup failed")
                    return
                try:
                    flags = os.O_WRONLY | os.O_CREAT
                    if append:
                        flags |= os.O_APPEND
                    else:
                        flags |= os.O_TRUNC
                    fd = os.open(out_file, flags, 0o666)
                    os.dup2(fd, 1)
                    os.close(fd)
                except:
                    self.print_error("smash error: open failed")
                    try:
                        os.close(saved_stdout)
                    except:
                        pass
                    return

                try:
                    self.run_builtin(cmd, args)
                finally:
                    os.dup2(saved_stdout, 1)
                    os.close(saved_stdout)
            else:
                self.run_builtin(cmd, args)
            return

        # External simple command (no pipes).
        self.run_external(line, background, original_line)

    # --------------------------------------------------------
    # LOOP
    # --------------------------------------------------------

    def loop(self):
        while True:
            try:
                self.cleanup_jobs()
                line = input(f"{self.prompt}> ")
            except EOFError:
                break
            except KeyboardInterrupt:
                print()
                continue

            self.execute_line(line)


# =====================================================
#  MAIN ENTRY
# =====================================================

if __name__ == "__main__":
    # If called with --sim → behave as simulator
    if len(sys.argv) >= 2 and sys.argv[1] == "--sim":
        shell = SmashShell()
        shell.loop()
    else:
        run_all_tests()
