from xmlrpc.server import SimpleXMLRPCServer, SimpleXMLRPCRequestHandler
from socketserver import ThreadingMixIn
import xmlrpc.client, threading, time
from datetime import datetime

class ThreadedXMLRPCServer(ThreadingMixIn, SimpleXMLRPCServer):
    pass

class RequestHandler(SimpleXMLRPCRequestHandler):
    rpc_paths = ('/RPC2',)

# -------- Configuration --------
PRIMARY_CAPACITY = 5
BUFFER_SIZE = 5
BACKUP_SERVER_URL = "http://127.0.0.1:8601/RPC2"
PORTAL_SERVER_URL = "http://127.0.0.1:8000/RPC2"

request_count = 0
buffer = []
lock = threading.Lock()

# ---------------- Utility Functions ----------------
def log_to_portal(msg):
    try:
        portal = xmlrpc.client.ServerProxy(PORTAL_SERVER_URL, allow_none=True)
        portal.receive_main_log(f"[Main Processor] {msg}")
    except Exception as e:
        print(f"Failed to send log to portal: {e}")

def process_on_main(req_id):
    global request_count
    log_to_portal(f"Processing student {req_id} on MAIN (request #{request_count})")
    time.sleep(2)  # Simulate processing time
    log_to_portal(f"Completed processing of {req_id} on MAIN.")
    return f"Processed by main: {req_id}"

def send_to_backup_and_log(batch):
    try:
        backup = xmlrpc.client.ServerProxy(BACKUP_SERVER_URL, allow_none=True)
        resp = backup.process_batch(batch)
        log_to_portal(f"Migrated students to BACKUP: {', '.join(batch)}")
    except Exception as e:
        log_to_portal(f"ERROR: Could not reach backup server. {e}")

# ---------------- XML-RPC Request ----------------
def process_request(req_id):
    global request_count, buffer
    with lock:
        request_count += 1
        if request_count <= PRIMARY_CAPACITY:
            threading.Thread(target=process_on_main, args=(req_id,)).start()
            return f"Queued for main processing."
        else:
            buffer.append(req_id)
            log_to_portal(f"Buffered student {req_id} for BACKUP.")
            if len(buffer) == BUFFER_SIZE:
                threading.Thread(target=send_to_backup_and_log, args=(buffer[:],)).start()
                buffer.clear()
            return f"Queued for backup processing."

# ---------------- Run Server ----------------
if __name__ == "__main__":
    server = ThreadedXMLRPCServer(("0.0.0.0", 8600), requestHandler=RequestHandler, allow_none=True)
    server.register_function(process_request, "process_request")
    log_to_portal("Main Processor Server started and ready to receive requests.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log_to_portal("Main Processor Server shutting down.")