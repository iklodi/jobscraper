import json
import os

PROGRESS_FILE = 'progress.json'
RESULT_FILE = 'last_run.json'

def set_status(stage, processed, total, awaiting_review=False):
    """awaiting_review marks a run that has finished its work and is only
    holding filled forms open, so the dashboard can say so instead of
    leaving a Stop button up that reads as 'still working'."""
    data = _read()
    data['is_running'] = True
    data['current_stage'] = stage
    data['processed'] = processed
    data['total'] = total
    data['awaiting_review'] = awaiting_review
    _write(data)


def set_result(summary):
    """The one-line outcome of the last finished run, for the dashboard."""
    try:
        with open(RESULT_FILE, 'w') as f:
            json.dump(summary, f)
    except Exception:
        pass


def take_result():
    """Read and clear the last result - it is shown once."""
    if not os.path.exists(RESULT_FILE):
        return None
    try:
        with open(RESULT_FILE) as f:
            data = json.load(f)
        os.remove(RESULT_FILE)
        return data
    except Exception:
        return None

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
            'stop_requested': False,
            'awaiting_review': False
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
            'stop_requested': False,
            'awaiting_review': False
        }

def _write(data):
    try:
        with open(PROGRESS_FILE, 'w') as f:
            json.dump(data, f)
    except Exception:
        pass
