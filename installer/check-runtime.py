"""Check runtime capabilities without interpolating installation paths into code."""
from pathlib import Path
import importlib.util
import sys

if sys.argv[1] == "pip":
    print(int(importlib.util.find_spec("pip") is not None))
elif sys.argv[1] == "health":
    root = Path(sys.argv[2]).resolve()
    sys.path.insert(0, str(root / "src"))
    import progress_wx
    import yaml
    import lark_channel
    import PIL
    assert Path(progress_wx.__file__).resolve().is_relative_to(root)
    assert progress_wx.__version__ == sys.argv[3]
elif sys.argv[1] == "service-state":
    root = Path(sys.argv[2]).resolve()
    sys.path.insert(0, str(root / "src"))
    from progress_wx.config import load_config
    from progress_wx.process_control import instance_running
    config = load_config(root / "config.yaml")
    print(int(instance_running(config.service.pid_file)))
else:
    raise ValueError("Unknown runtime check")
