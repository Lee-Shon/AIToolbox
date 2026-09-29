"""Real Windows process ownership, without loading a model."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import unittest

from local_product.ownership import ProcessJob


@unittest.skipUnless(os.name == "nt", "Windows product")
class Ownership(unittest.TestCase):
    def test_close_kills_only_owned_child(self):
        job = ProcessJob()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        external = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            job.assign(child)
            job.close()
            child.wait(timeout=5)
            self.assertIsNone(external.poll())
            job.close()
        finally:
            job.close()
            for process in (child, external):
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)

    def test_abrupt_parent_exit_reclaims_assigned_native(self):
        root = str(Path(sys.modules[ProcessJob.__module__].__file__).resolve().parents[1])
        code = "\n".join([
            "import sys, subprocess, time",
            "sys.path.insert(0, " + repr(root) + ")",
            "from local_product.ownership import ProcessJob",
            "job = ProcessJob()",
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])",
            "job.assign(p)",
            "print(p.pid, flush=True)",
            "time.sleep(30)",
        ])
        parent = subprocess.Popen([sys.executable, "-B", "-c", code], stdout=subprocess.PIPE, text=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = None
        try:
            pid = int(parent.stdout.readline())
            handle = kernel.OpenProcess(0x100000, False, pid)
            self.assertTrue(handle)
            self.assertEqual(kernel.WaitForSingleObject(handle, 0), 258)
            parent.kill()
            parent.wait(timeout=5)
            self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0)
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=5)
            parent.stdout.close()
            if handle:
                kernel.CloseHandle(handle)
