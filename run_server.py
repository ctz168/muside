#!/usr/bin/env python3
import os
os.environ['MUSIDE_PORT'] = '1239'
from muside_server import app
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=1239, debug=False, threaded=True, use_reloader=False)
