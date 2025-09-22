# client_requests.py
import requests
import time
from datetime import datetime

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

MAIN_PROCESSOR_URL = "http://127.0.0.1:5000/process_request"

student_ids = [
    '24102A2001', '24102A2002', '24102A2003', '24102A2004', '24102A2005',
    '24102A2006', '24102A2007', '24102A2008', '24102A2009', '24102A2010'
]

log("Client: Sending 10 student requests to the main web portal...")
for student_id in student_ids:
    log(f"Client: Sending request for student {student_id}")
    try:
        resp = requests.post(MAIN_PROCESSOR_URL, json={"req_id": student_id})
        log(f"Client: Response for {student_id} -> {resp.json().get('status', 'Error')}")
    except Exception as e:
        log(f"Client: ERROR for {student_id} -> {e}")
    time.sleep(1)