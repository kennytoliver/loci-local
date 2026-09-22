import time, sys
sys.path.insert(0, r"C:/Users/user/Hippocampus")
import hippocampus as hippo

for name, fn in [("quality_scan", hippo.quality_scan), ("health_score", hippo.health_score),
                 ("audit_report", hippo.audit_report)]:
    ts = []
    for _ in range(3):
        t0 = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t0)
    print(f"{name:16s} " + " ".join(f"{t:.3f}s" for t in ts))

print()
import inspect
src = inspect.getsource(hippo.health_score)
print("=== health_score 源码（看它是否内部又调了 quality_scan）===")
print(src[:900])
