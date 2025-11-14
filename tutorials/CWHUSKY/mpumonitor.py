import serial
import threading
import time
import re
import json
from collections import deque
from datetime import datetime

# Exactly this line (start of TF-A boot)
RE_CPU_RESET = re.compile(r'^NOTICE:\s+CPU:\s+STM32MP257FAI\s+Rev\.Y\b')
RE_BOOT_FINISHED = re.compile(r'^## Starting application at 0x88000040\b')

RE_CRASH_START = re.compile(r"(Synchronous Abort|Undefined Instruction|Exception)", re.I)
RE_CRASH_END = re.compile(r"^Code: .* \(.*\)")

RE_ELR = re.compile(r"elr:\s*([0-9A-Fa-fx]+)")
RE_LR  = re.compile(r"lr\s*:\s*([0-9A-Fa-fx]+)")
RE_ESR = re.compile(r"esr\s*(0x[0-9A-Fa-fA-F]+)")


class SerialMonitor:
    def __init__(self, port="/dev/ttyACM2", baud=115200, max_log=5000,
                 crash_db_path="crash_db.json"):
        self.port = port
        self.baud = baud

        # Control flags
        self.print_enabled = False
        self.running = False

        # State
        self.state = "unknown"   # booting / running / unknown
        self.log_buffer = deque(maxlen=max_log)

        # Crash capture
        self.crash_active = False
        self.crash_temp = []
        self.last_crash = []

        # Run tracking
        self.run_id = None
        self.run_counter = 0  # increments each boot

        # Crash database
        self.crash_db_path = crash_db_path
        self.crash_db = {}  # run_id -> crash info

        # Thread handle
        self.thread = None

    # ---------------- Public API ----------------
    def start(self):
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self._thread_loop, daemon=True)
            self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join()

    def set_print(self, enabled: bool):
        self.print_enabled = enabled

    def get_state(self):
        return self.state

    def get_crash(self):
        return list(self.last_crash)

    def get_logs(self):
        return list(self.log_buffer)

    def set_run_id(self, rid: str):
        self.run_id = rid

    def save_db(self):
        if not self.crash_db_path:
            return
        with open(self.crash_db_path, "w") as f:
            json.dump(self.crash_db, f, indent=2)

    def load_db(self):
        if not self.crash_db_path:
            self.crash_db = {}
            self.run_counter = 0
            return

        try:
            with open(self.crash_db_path, "r") as f:
                self.crash_db = json.load(f)
            # restore run_counter from existing keys
            max_n = 0
            for k in self.crash_db.keys():
                if k.startswith("run_"):
                    try:
                        n = int(k.split("_", 1)[1])
                        max_n = max(max_n, n)
                    except ValueError:
                        pass
            self.run_counter = max_n
        except FileNotFoundError:
            self.crash_db = {}
            self.run_counter = 0

    # ---------------- Thread Loop ----------------
    def _thread_loop(self):
        try:
            ser = serial.Serial(self.port, self.baud, timeout=0.2)
        except Exception as e:
            print(f"[SerialMonitor] Serial open error: {e}")
            self.running = False
            return

        while self.running:
            try:
                line = ser.readline().decode(errors="ignore").rstrip()
                if not line:
                    continue
                self._process_line(line)
                if self.print_enabled:
                    print(line)
            except Exception as e:
                print(f"[SerialMonitor] Read error: {e}")
                time.sleep(0.2)

        ser.close()

    # ---------------- Crash Parsing Utils ----------------
    def _extract_elr(self, crash):
        for l in crash:
            m = RE_ELR.search(l)
            if m:
                return m.group(1)
        return None

    def _extract_lr(self, crash):
        for l in crash:
            m = RE_LR.search(l)
            if m:
                return m.group(1)
        return None

    def _extract_esr(self, crash):
        for l in crash:
            m = RE_ESR.search(l)
            if m:
                return m.group(1)
        return None

    def _extract_code(self, crash):
        for l in crash:
            if l.startswith("Code:"):
                # Code: aaaaaaaa bbbbbbbb cccccccc dddddddd (eeeeeeee)
                parts = re.findall(r"[0-9a-fA-F]{8}", l)
                if not parts:
                    return None, None
                prev = parts[:-1]
                fault = parts[-1]
                return prev, fault
        return None, None

    # ---------------- Line Processor ----------------
    def _process_line(self, line: str):

        # ---------- 1. Detect CPU RESET ----------
        if RE_CPU_RESET.search(line):

            # If crash ongoing, finalize partial crash
            if self.crash_active:
                self._finalize_crash(complete=False)

            # NEW BOOT = increment run counter
            self.run_counter += 1
            self.run_id = f"run_{self.run_counter}"

            self.log_buffer.clear()
            self.state = "booting"
            self.log_buffer.append(line)
            return

        # Normal log storage
        self.log_buffer.append(line)

        # ---------- 2. Crash dump detection ----------
        if RE_CRASH_START.search(line):
            self.crash_active = True
            self.crash_temp = []

        if self.crash_active:
            self.crash_temp.append(line)

            if RE_CRASH_END.search(line):
                self._finalize_crash(complete=True)
            return

        # ---------- 3. Boot finished detection ----------
        if RE_BOOT_FINISHED.search(line):
            self.state = "running"

    # ---------------- Crash Finalization ----------------
    def _finalize_crash(self, complete=True):
        self.crash_active = False
        self.last_crash = list(self.crash_temp)

        elr = self._extract_elr(self.crash_temp)
        lr  = self._extract_lr(self.crash_temp)
        esr = self._extract_esr(self.crash_temp)
        prev_instrs, fault_instr = self._extract_code(self.crash_temp)

        crash_entry = {
            "timestamp": datetime.now().isoformat(),
            "run_id": self.run_id,
            "complete": bool(complete),
            "esr": esr if esr else None,
            "elr": elr if elr else None,
            "lr": lr if lr else None,
            "prev_instructions": prev_instrs if prev_instrs else None,
            "faulting_instruction": fault_instr if fault_instr else None,
            "raw_dump": self.last_crash,
        }

        if self.run_id is None:
            # fallback run_id if no boot banner seen yet
            self.run_counter += 1
            self.run_id = f"run_{self.run_counter}"

        self.crash_db[self.run_id] = crash_entry
        self.save_db()

        self.crash_temp = []
