import os, sys, tempfile
os.environ["DATA_DIR"] = tempfile.mkdtemp()
for k in ("GOOGLE_VISION_API_KEY", "TINEYE_API_KEY", "SERPAPI_API_KEY", "PUBLIC_BASE_URL"):
    os.environ[k] = ""
os.environ["RESPECT_ROBOTS_TXT"] = "false"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
