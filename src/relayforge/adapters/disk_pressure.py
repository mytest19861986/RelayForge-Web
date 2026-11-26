import os
import shutil


def disk_state():
    usage = shutil.disk_usage(os.environ.get("RF_MEDIA_STAGING_ROOT", "/tmp"))
    ratio = usage.used / usage.total
    return "CRITICAL" if ratio >= 0.95 else "WARNING" if ratio >= 0.85 else "NORMAL"
