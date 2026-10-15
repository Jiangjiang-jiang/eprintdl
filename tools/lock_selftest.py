from __future__ import annotations

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from eprintdl.locking import ChromeLock, LockTimeout, pick_free_port

LOCK = os.path.join(tempfile.gettempdir(), "eprintdl-lock-selftest.lock")


def main() -> None:
    if os.path.exists(LOCK):
        os.remove(LOCK)

    print("[1] 端口分配")
    ports = {pick_free_port(9300, 9310) for _ in range(3)}
    print("    连续三次分配:", sorted(ports))

    print("[2] 第一个持锁者")
    first = ChromeLock(LOCK, timeout=5, port_range=(9300, 9310))
    first.__enter__()
    print("    holder port =", first.port, "| lock 存在 =", os.path.exists(LOCK))

    print("[3] 第二个调用者（超时 2s，应失败并给出持有者信息）")
    started = time.time()
    try:
        with ChromeLock(LOCK, timeout=2, port_range=(9300, 9310)) as second:
            print("    失败：不应该拿到锁:", second.port)
    except LockTimeout as exc:
        print(f"    通过：LockTimeout（等了 {time.time()-started:.1f}s）: {str(exc)[:90]}...")

    print("[4] 释放后应可获取，并拿到不同端口")
    first.__exit__(None, None, None)
    print("    释放后 lock 存在 =", os.path.exists(LOCK))
    with ChromeLock(LOCK, timeout=5, port_range=(9300, 9310)) as third:
        print("    通过：新持有者 port =", third.port)
    print("    退出后 lock 存在 =", os.path.exists(LOCK))


if __name__ == "__main__":
    main()
