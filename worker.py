"""Dedicated database-backed generation worker.

Run with EMBEDDED_WORKER=0 on the web process and start this module separately:
    python worker.py
"""

from main import run_worker_forever


if __name__ == "__main__":
    run_worker_forever()
