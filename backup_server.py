from xmlrpc.server import SimpleXMLRPCServer, SimpleXMLRPCRequestHandler
from socketserver import ThreadingMixIn
import xmlrpc.client, time
from datetime import datetime

class ThreadedXMLRPCServer(ThreadingMixIn, SimpleXMLRPCServer):
    pass

class RequestHandler(SimpleXMLRPCRequestHandler):
    rpc_paths = ('/RPC2',)

# -------- Configuration --------
BACKUP_PROCESS_TIME = 2
PORTAL_SERVER_URL = "http://127.0.0.1:8000/RPC2"
backup_status = {}

def log_to_portal(msg):
    try:
        portal = xmlrpc.client.ServerProxy(PORTAL_SERVER_URL, allow_none=True)
        portal.receive_backup_log(msg)
    except Exception as e:
        print(f"Failed to send log to portal: {e}")

def process_batch(req_list):
    log_to_portal(f"Received batch of {len(req_list)} students.")
    for req_id in req_list:
        _process_request(req_id)
    return f"Batch processed: {req_list}"

def _process_request(req_id):
    backup_status[req_id] = "processing"
    log_to_portal(f"Processing student {req_id} on BACKUP.")
    time.sleep(BACKUP_PROCESS_TIME)
    backup_status[req_id] = "completed"
    log_to_portal(f"Completed processing of {req_id} on BACKUP.")

if __name__ == "__main__":
    server = ThreadedXMLRPCServer(("0.0.0.0", 8601), requestHandler=RequestHandler, allow_none=True)
    server.register_function(process_batch, "process_batch")
    log_to_portal("Backup Processor Server started and ready to receive batches.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log_to_portal("Backup Processor Server shutting down.")