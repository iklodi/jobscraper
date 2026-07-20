import json
import os

PROGRESS_FILE = 'progress.json'

def set_status(stage, processed, total):
    data = _read()
    data['is_running'] = True
    data['current_stage'] = stage
    data['processed'] = processed
    data['total'] = total
    _write(data)

def is_stop_requested():
    data = _read()
    return data.get('stop_requested', False)

def request_stop():
    data = _read()
    data['stop_requested'] = True
    _write(data)

def clear_status():
    if os.path.exists(PROGRESS_FILE):
        try:
            os.remove(PROGRESS_FILE)
        except:
            pass

def get_status():
    return _read()

def _read():
    if not os.path.exists(PROGRESS_FILE):
        return {
            'is_running': False,
            'current_stage': '',
            'processed': 0,
            'total': 0,
            'stop_requested': False
        }
    try:
        with open(PROGRESS_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {
            'is_running': False,
            'current_stage': '',
            'processed': 0,
            'total': 0,
            'stop_requested': False
        }

def _write(data):
    try:
        with open(PROGRESS_FILE, 'w') as f:
            json.dump(data, f)
    except Exception:
        pass
