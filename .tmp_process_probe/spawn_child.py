import subprocess, sys, time
from pathlib import Path
pid_file = Path(r'G:/mycode/CoreCoder/.tmp_process_probe/child.pid')
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
pid_file.write_text(str(child.pid), encoding='utf-8')
print(f"PARENT_READY:{child.pid}", flush=True)
time.sleep(120)
