import serial
import threading
import time
import re
import json
from collections import deque
from datetime import datetime



RE_CPU_RESET = re.compile(r"NOTICE:\s+CPU:\s+STM32MP257", re.I)
RE_BOOT_FINISHED = re.compile(r"Starting application at 0x88000040", re.I)
RE_CRASH_START = re.compile(r"(Synchronous Abort|Undefined Instruction|Exception)", re.I)
RE_CRASH_END = re.compile(r"^Code: .* \(.*\)")
RE_ELR = re.compile(r"elr:\s*([0-9A-Fa-fx]+)")
RE_LR  = re.compile(r"lr\s*:\s*([0-9A-Fa-fx]+)")
RE_ESR = re.compile(r"esr\s*(0x[0-9A-Fa-fA-F]+)")


class SerialMonitor:
    def __init__(self, port="/dev/ttyACM2", baud=115200, max_log=5000):
        self.port = port
        self.baud = baud

        # Control flags
        self.print_enabled = False
        self.running = False

        # State
        self.state = "unknown"   # booting / running / unknown
        self.log_buffer = deque(maxlen=max_log)

        # Crash dump
        self.crash_active = False
        self.crash_temp = []
        self.last_crash = []

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

    # ---------------- Line Processor ----------------
    def _process_line(self, line: str):
        
        # ---------- 1. Detect CPU RESET ----------
        if RE_CPU_RESET.search(line):

            # If we were in the middle of capturing a crash,
            # finalize whatever partial crash dump we have.
            if self.crash_active:
                self.last_crash = list(self.crash_temp)
                self.crash_temp = []
                self.crash_active = False

            # Now clear logs and reset state for the new boot
            self.log_buffer.clear()
            self.state = "booting"

            # Continue storing CPU reset line
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
                # crash dump finished
                self.crash_active = False
                self.last_crash = list(self.crash_temp)
                self.crash_temp = []
            return

        # ---------- 3. Boot finished detection ----------
        if RE_BOOT_FINISHED.search(line):
            self.state = "running"