import os, time
from pathlib import Path
Path(r'G:\mycode\CoreCoder\tmp\process_repro\child.pid').write_text(str(os.getpid()), encoding='utf-8')
print('ready', flush=True)
time.sleep(30)
