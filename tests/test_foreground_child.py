"""Ctrl+C belongs to the foreground CLI, not the wrapper."""

import signal
import subprocess
import sys
import threading

from claudapter.command import foreground_child


def test_sigint_during_child_does_not_interrupt_wrapper_and_handler_is_restored():
    before = signal.getsignal(signal.SIGINT)
    timer = threading.Timer(0.3, signal.raise_signal, (signal.SIGINT,))
    with foreground_child():
        timer.start()
        result = subprocess.run([sys.executable, "-c", "import time; time.sleep(1.2); print('done')"], capture_output=True, text=True, check=False)
    timer.join()
    assert result.returncode == 0 and result.stdout.strip() == "done"
    assert signal.getsignal(signal.SIGINT) is before


def test_off_main_thread_is_a_no_op():
    seen = []

    def worker():
        with foreground_child():
            seen.append(signal.getsignal(signal.SIGINT))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert seen == [signal.getsignal(signal.SIGINT)]
