import sys
from pathlib import Path
from autocode.infra.processes import BackgroundProcessManager
root = Path(r'G:/mycode/CoreCoder/.tmp_process_probe_leak')
info_file = Path(r'G:/mycode/CoreCoder/.tmp_process_probe_leak/info.txt')
child_script = root / 'child_sleep.py'
child_script.write_text('import time\ntime.sleep(120)\n', encoding='utf-8')
mgr = BackgroundProcessManager(str(root))
started = mgr.start_process(command=f'"{sys.executable}" "{child_script}"', cwd='.')
info_file.write_text(started, encoding='utf-8')
print(started, flush=True)
