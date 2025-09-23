from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
from datetime import datetime, timedelta
import time
import random
from threading import Thread, Lock, Timer, Semaphore, Condition
import logging
import json, os, threading
import xmlrpc.client
from xmlrpc.server import SimpleXMLRPCServer, SimpleXMLRPCRequestHandler
from socketserver import ThreadingMixIn

# --- SETUP ---
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key!'
socketio = SocketIO(app)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# --- WEB PORTAL LOGIC ---
student_dataset = {
    '24102A2001': 'SIDDHI GAWADE', '24102A2002': 'SANDEEP MAJUMDAR',
    '24102A2003': 'CHIRAG CHAUDHARI', '24102A2004': 'ANUSHKA UNDE',
    '24102A2005': 'SHAZIYA SHAIKH', '24102A2006': 'TAMANNA SHAIKH',
    '24102A2007': 'TANSHIQ KULKARNI', '24102A2008': 'ARAV MAHIND',
    '24102A2009': 'PREM DESHMUKH', '24102A2010': 'ISHA BANSAL',
}

def reset_student_data():
    return {
        roll_no: {'name': name, 'marks': 0, 'cheating_count': 0, 'exam_terminated': False}
        for roll_no, name in student_dataset.items()
    }

students_data = reset_student_data()
StatusDB = {}
SubmissionDB = {}
exam_active = False
exam_timer = None
cheating_thread = None
lock = Lock()
c = 0  # <-- Add this line to initialize c

# --- TASK 7 Load Balancing State ---
PRIMARY_CAPACITY = 5
BUFFER_SIZE = 5
main_processor_requests_count = 0
main_processor_buffer = []
main_processor_lock = Lock()
main_processor_semaphore = Semaphore(PRIMARY_CAPACITY)

# --- NEW: TASK 8 Replicated DB State ---
PRIMARY_FILE = "primary_db.json"
REPLICA_FILE = "replica_db.json"
METADATA_FILE = "metadata.json"
processor_server = None

# --- RPC Endpoints for receiving logs from processors ---
class RequestHandler(SimpleXMLRPCRequestHandler):
    rpc_paths = ('/RPC2',)

def receive_main_log(msg):
    log_time = datetime.now().strftime('%H:%M:%S')
    socketio.emit('main_log', {'log': f"[{log_time}] {msg}"})
    return True

def receive_backup_log(msg):
    log_time = datetime.now().strftime('%H:%M:%S')
    socketio.emit('backup_log', {'log': f"[{log_time}] {msg}"})
    return True

class ThreadedXMLRPCServer(Thread, SimpleXMLRPCServer):
    def __init__(self, host, port):
        SimpleXMLRPCServer.__init__(self, (host, port), requestHandler=RequestHandler)
        Thread.__init__(self)
        self.daemon = True

    def run(self):
        self.register_function(receive_main_log, "receive_main_log")
        self.register_function(receive_backup_log, "receive_backup_log")
        print("Web Portal RPC Server running on port 8000...")
        self.serve_forever()

# --- ROUTES FOR WEBPAGES ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/teacher')
def teacher_view():
    return render_template('teacher.html')

@app.route('/student')
def student_view():
    return render_template('student.html', students=student_dataset)

@app.route('/processor')
def processor_view():
    return render_template('processor.html')

@app.route('/backup_server')
def backup_server_view():
    return render_template('backup_server.html')

@app.route('/replicated_db')
def replicated_db_view():
    return render_template('replicated_db.html')

@app.route('/clock_sync')
def clock_sync_view():
    return render_template('clock_sync.html')


# --- EXAM SUBMISSION API ENDPOINTS ---
@app.route("/start_exam", methods=["POST"])
def start_exam():
    global exam_active, StatusDB, SubmissionDB, exam_timer, cheating_thread, students_data, main_processor_requests_count, main_processor_buffer
    students_data = reset_student_data()
    socketio.emit('marks_update', list(students_data.items()))
    data = request.json
    students = data.get("students", [])
    duration = data.get("duration", 60)
    with lock:
        if exam_active:
            return jsonify({"error": "Exam is already active"}), 400
        StatusDB = {sid: False for sid in students}
        SubmissionDB.clear()
        exam_active = True
    if exam_timer and exam_timer.is_alive():
        exam_timer.cancel()
    exam_timer = Timer(duration, auto_submit_all)
    exam_timer.start()
    if cheating_thread is None or not cheating_thread.is_alive():
        cheating_thread = Thread(target=cheating_simulation)
        cheating_thread.daemon = True
        cheating_thread.start()
    log_msg = f"Exam started for {len(students)} students. Duration: {duration} seconds."
    socketio.emit('main_log', {'log': log_msg})
    socketio.emit('exam_status', {'status': 'started', 'duration': duration, 'students': students})
    socketio.emit('submission_status_update', StatusDB)
    main_processor_requests_count = 0
    main_processor_buffer.clear()
    return jsonify({"msg": "Exam started"})

def auto_submit_all():
    global exam_active
    with lock:
        if not exam_active:
            return
        for sid, submitted in StatusDB.items():
            if not submitted:
                SubmissionDB[sid] = {
                    "answers": {},
                    "auto": True,
                    "marks": 0,
                    "submitted_at": datetime.now().isoformat(),
                    "name": student_dataset.get(sid, "Unknown")
                }
                StatusDB[sid] = True
                # Update student marks in students_data for teacher's marksheet
                if sid in students_data:
                    students_data[sid]['marks'] = 0
        exam_active = False
    log_msg = "Exam ended. All pending submissions have been auto-submitted."
    socketio.emit('processor_log', {'log': log_msg})
    socketio.emit('exam_status', {'status': 'ended'})
    socketio.emit('submission_update', SubmissionDB)
    socketio.emit('marks_update', list(students_data.items()))  # Update teacher's marksheet

@app.route("/manual_submit", methods=["POST"])
def manual_submit():
    global c
    data = request.json
    sid = data["student_id"]
    answers = data["answers"]
    
    # Correct answers for MCQ questions (each worth 1 mark)
    correct_answers = {
        'q1': 'c',
        'q2': 'c',
        'q3': 'c',
        'q4': 'b',
        'q5': 'c'
    }
    
    with lock:
        if not exam_active:
            return jsonify({"error": "Exam not active"}), 400
        if sid not in StatusDB:
            return jsonify({"error": "Unknown student"}), 400
        if StatusDB[sid]:
            return jsonify({"error": "Already submitted"}), 400
        
        marks = 0
        for question, student_answer in answers.items():
            if question in correct_answers and student_answer == correct_answers[question]:
                marks += 1
        
        SubmissionDB[sid] = {
            "answers": answers,
            "auto": False,
            "marks": marks,
            "submitted_at": datetime.now().isoformat(),
            "name": student_dataset.get(sid, "Unknown")
        }
        StatusDB[sid] = True

    # Load balancing check
    if c < 2:
        log_msg = f"Student {sid} submitted exam manually. Marks: {marks} (Processed by MAIN)"
        socketio.emit('main_log', {'log': log_msg})
    else:
        log_msg = f"Student {sid} submitted exam manually. Marks: {marks} (Processed by BACKUP)"
        socketio.emit('backup_log', {'log': log_msg})
        # Also notify on main which student migrated
        socketio.emit('main_log', {'log': f"Student {sid} migrated to BACKUP server."})

    c += 1  # Increment c after each submission

    socketio.emit('student_notification', {'message': f"Your exam has been submitted successfully.", 'type': 'success', 'timestamp': time.strftime('%H:%M:%S')})
    socketio.emit('submission_update', SubmissionDB)
    socketio.emit('submission_status_update', StatusDB)
    socketio.emit('marks_update', list(students_data.items()))
    return jsonify({"msg": "Submitted successfully", "marks": marks})

# --- TASK 7 Load Balancing Logic ---
@app.route("/process_request", methods=["POST"])
def process_request():
    global main_processor_requests_count
    data = request.json
    req_id = data["req_id"]
    
    with main_processor_lock:
        main_processor_requests_count += 1
        current_count = main_processor_requests_count
    
    if main_processor_semaphore.acquire(blocking=False):
        Thread(target=process_on_main, args=(req_id, current_count,)).start()
        return jsonify({"status": f"Queued for main processing."})
    else:
        main_processor_buffer.append(req_id)
        log_message = f"Student {req_id} added to buffer. Size: {len(main_processor_buffer)}/{BUFFER_SIZE}"
        socketio.emit('main_log', {'log': log_message})
        
        if len(main_processor_buffer) == BUFFER_SIZE:
            log_message = f"Buffer full. Sending batch of {len(main_processor_buffer)} requests to Backup Server."
            socketio.emit('main_log', {'log': log_message})
            
            backup_processor_thread = Thread(target=process_batch_backup, args=(main_processor_buffer[:],)).start()
            
            main_processor_buffer.clear()
        
        return jsonify({"status": f"Queued for backup: {req_id}"})

def process_on_main(req_id, current_count):
    log_message = f"Processing student {req_id} on MAIN (request #{current_count})"
    socketio.emit('main_log', {'log': log_message})
    time.sleep(2)
    log_message = f"Completed processing of {req_id} on MAIN."
    socketio.emit('main_log', {'log': log_message})
    main_processor_semaphore.release()

def process_batch_backup(req_list):
    try:
        backup = xmlrpc.client.ServerProxy("http://127.0.0.1:8601/RPC2", allow_none=True)
        resp = backup.process_batch(req_list)
        socketio.emit('main_log', {'log': f"Sent batch to backup server: {resp}"})
    except Exception as e:
        socketio.emit('main_log', {'log': f"ERROR: Could not reach backup server. {e}"})

# --- BACKGROUND THREAD FOR CHEATING SIMULATION ---
def cheating_simulation():
    socketio.sleep(3)
    log_message = 'Cheating simulation background thread started.'
    socketio.emit('processor_log', {'log': log_message})
    while True:
        if not exam_active:
            log_message = "Exam ended. Cheating simulation stopped."
            socketio.emit('processor_log', {'log': log_message})
            break
        active_students = [
            roll_no for roll_no, data in students_data.items() if not data['exam_terminated']
        ]
        if not active_students:
            log_message = "All student exams have been terminated. Simulation stopped."
            socketio.emit('processor_log', {'log': log_message})
            break
        cheater_roll_no = random.choice(active_students)
        student = students_data[cheater_roll_no]
        student['cheating_count'] += 1
        notification_for_student = {}
        if student['cheating_count'] == 1:
            student['marks'] = 50
            notification_for_student = {
                'message': f"1st cheating detected for {student['name']}. Your marks have been reduced to 50.",
                'type': 'warning', 'timestamp': time.strftime('%H:%M:%S')
            }
        elif student['cheating_count'] >= 2:
            student['marks'] = 0
            student['exam_terminated'] = True
            notification_for_student = {
                'message': f"2nd cheating detected for {student['name']}. Your exam has been terminated.",
                'type': 'danger', 'timestamp': time.strftime('%H:%M:%S')
            }
        log_message = f"Cheating detected for {student['name']} ({cheater_roll_no})."
        socketio.emit('processor_log', {'log': log_message})
        socketio.emit('student_notification', notification_for_student)
        socketio.emit('marks_update', list(students_data.items()))
        socketio.sleep(random.randint(5, 10))

# --- NEW: TASK 8 REPLICATED DB LOGIC ---
class RWLock:
    def __init__(self):
        self.lock = threading.Lock()
        self.read_ready = threading.Condition(self.lock)
        self.readers = 0
        self.writer_active = False
        self.rw_lock = Lock()
        self.read_cond = Condition(self.rw_lock)
        self.write_cond = Condition(self.rw_lock)

    def try_acquire_read(self):
        with self.lock:
            if self.writer_active:
                return False
            self.readers += 1
            return True

    def release_read(self):
        with self.lock:
            self.readers -= 1
            if self.readers == 0:
                self.write_cond.notify()

    def try_acquire_write(self):
        with self.lock:
            if self.writer_active or self.readers > 0:
                return False
            self.writer_active = True
            return True

    def release_write(self):
        with self.lock:
            self.writer_active = False
            self.read_ready.notify_all()
            self.write_cond.notify()

class ProcessorServer:
    def __init__(self, total_students=28, chunks=4):
        self.total_students = total_students
        self.chunks = chunks
        self.primary = {}
        self.replica = {}
        self.metadata = {}
        self.chunk_locks = {i: RWLock() for i in range(chunks)}
        self._init_or_load()

    def _init_or_load(self):
        if not os.path.exists(PRIMARY_FILE):
            self._init_dummy_data()
        with open(PRIMARY_FILE, 'r') as f:
            self.primary = json.load(f)
        with open(REPLICA_FILE, 'r') as f:
            self.replica = json.load(f)
        with open(METADATA_FILE, 'r') as f:
            self.metadata = json.load(f)

    def _init_dummy_data(self):
        # Use student_dataset from the main app
        names = list(student_dataset.values())
        primary = {}
        for idx, roll_no in enumerate(student_dataset.keys()):
            IAS = random.randint(10, 30)
            MSE = random.randint(20, 30)
            ESE = random.randint(30, 50)
            total = IAS + MSE + ESE
            primary[roll_no] = {
                "roll": roll_no,
                "name": names[idx],
                "marks": {"IAS": IAS, "MSE": MSE, "ESE": ESE, "total": total}
            }
        replica = {k: dict(v) for k, v in primary.items()}
        metadata = {"chunks": []}
        size = len(student_dataset) // self.chunks
        roll_nos = list(student_dataset.keys())
        for i in range(self.chunks):
            start = i * size
            end = (i + 1) * size
            metadata["chunks"].append({"id": i, "range": roll_nos[start:end]})
        with open(PRIMARY_FILE, 'w') as f:
            json.dump(primary, f, indent=2)
        with open(REPLICA_FILE, 'w') as f:
            json.dump(replica, f, indent=2)
        with open(METADATA_FILE, 'w') as f:
            json.dump(metadata, f, indent=2)

    def _save(self):
        with open(PRIMARY_FILE, 'w') as f:
            json.dump(self.primary, f, indent=2)
        with open(REPLICA_FILE, 'w') as f:
            json.dump(self.replica, f, indent=2)

    def _chunk_for_roll(self, roll):
        roll_nos = list(student_dataset.keys())
        idx = roll_nos.index(roll)
        return idx // (self.total_students // self.chunks)

# --- TASK 8 API ENDPOINTS ---
@app.route("/read", methods=["GET"])
def read_db():
    roll = request.args.get("roll")
    if not roll or roll not in processor_server.primary:
        return jsonify({"error": "Roll not found"}), 404
    cid = processor_server._chunk_for_roll(roll)
    lock = processor_server.chunk_locks[cid]
    if not lock.try_acquire_read():
        return jsonify({"error": f"Roll {roll} is locked, try later"}), 423
    try:
        rec = processor_server.primary[roll]
        socketio.emit('db_log', {'log': f"{roll} is READING (Chunk {cid}). Readers: {lock.readers}"})
        return jsonify(rec), 200
    finally:
        lock.release_read()

@app.route("/lock", methods=["POST"])
def lock_db():
    data = request.json
    roll = str(data["roll"])
    if roll not in processor_server.primary:
        return jsonify({"error": f"Roll {roll} not found"}), 404
    cid = processor_server._chunk_for_roll(roll)
    lock = processor_server.chunk_locks[cid]
    if not lock.try_acquire_write():
        return jsonify({"error": f"Roll {roll} already locked"}), 423
    return jsonify({"success": True, "msg": f"Roll {roll} locked for update"})

@app.route("/unlock", methods=["POST"])
def unlock_db():
    data = request.json
    roll = str(data["roll"])
    if roll not in processor_server.primary:
        return jsonify({"error": f"Roll {roll} not found"}), 404
    cid = processor_server._chunk_for_roll(roll)
    lock = processor_server.chunk_locks[cid]
    lock.release_write()
    return jsonify({"success": True, "msg": f"Roll {roll} unlocked"})

@app.route("/write", methods=["POST"])
def write_db():
    data = request.json
    roll = str(data["roll"])
    new_marks = data["marks"]
    if roll not in processor_server.primary:
        return jsonify({"error": f"Roll {roll} not found"}), 404
    cid = processor_server._chunk_for_roll(roll)
    lock = processor_server.chunk_locks[cid]
    if not lock.writer_active:
        return jsonify({"error": f"Roll {roll} is not locked for update"}), 423
    try:
        rec = processor_server.primary[roll]
        rec["marks"]["IAS"] = new_marks.get("IAS", rec["marks"]["IAS"])
        rec["marks"]["MSE"] = new_marks.get("MSE", rec["marks"]["MSE"])
        rec["marks"]["ESE"] = new_marks.get("ESE", rec["marks"]["ESE"])
        rec["marks"]["total"] = (
            rec["marks"]["IAS"] + rec["marks"]["MSE"] + rec["marks"]["ESE"]
        )
        processor_server.replica[roll] = dict(rec)
        processor_server._save()
        socketio.emit('db_log', {'log': f"{roll} is WRITING (Chunk {cid}). Writer active: {lock.writer_active}"})
        socketio.emit('db_state', {
            'primary': processor_server.primary,
            'replica': processor_server.replica
        })
        return jsonify({"success": True, "record": rec})
    finally:
        lock.release_write()
    # After every read or write
    socketio.emit('db_state', {
        'primary': processor_server.primary,
        'replica': processor_server.replica
    })

# --- SOCKET.IO CONNECTION ---
@socketio.on('connect')
def handle_connect():
    log_message = "A new client connected to the website."
    socketio.emit('processor_log', {'log': log_message})

# --- CLOCK SYNCHRONIZATION SOCKET.IO EVENTS ---
@socketio.on('start_berkeley_demo')
def handle_start_berkeley():
    global berkeley_active, berkeley_server_time, berkeley_clients
    berkeley_active = True
    berkeley_server_time = datetime.now().strftime('%H:%M:%S')
    berkeley_clients = {}
    
    socketio.emit('berkeley_log', {'message': 'Berkeley Algorithm demonstration started'})
    socketio.emit('berkeley_status_update', {
        'server_time': berkeley_server_time,
        'clients': []
    })
    
    # Simulate client connections and CV calculations
    def simulate_berkeley():
        import time
        time.sleep(2)
        
        # Step 1: Server broadcasts time
        socketio.emit('berkeley_log', {'message': f'Step 1: Server broadcasts time {berkeley_server_time}'})
        time.sleep(1)
        
        # Step 2-3: Clients calculate and send CVs
        student_cv = random.randint(-30, 30)
        teacher_cv = random.randint(-30, 30)
        
        berkeley_clients['Student'] = {'cv': student_cv}
        berkeley_clients['Teacher'] = {'cv': teacher_cv}
        
        socketio.emit('berkeley_log', {'message': f'Step 2-3: Student CV = {student_cv}s, Teacher CV = {teacher_cv}s'})
        socketio.emit('berkeley_status_update', {
            'clients': [
                {'name': 'Student', 'cv': student_cv},
                {'name': 'Teacher', 'cv': teacher_cv}
            ]
        })
        time.sleep(2)
        
        # Step 4: Calculate average
        avg_cv = (student_cv + teacher_cv) / 2
        socketio.emit('berkeley_log', {'message': f'Step 4: Average CV = {avg_cv:.2f}s'})
        time.sleep(1)
        
        # Step 5: Calculate CAPs
        student_cap = avg_cv - student_cv
        teacher_cap = avg_cv - teacher_cv
        socketio.emit('berkeley_log', {'message': f'Step 5: Student CAP = {student_cap:.2f}s, Teacher CAP = {teacher_cap:.2f}s'})
        time.sleep(1)
        
        # Step 6-7: Clients adjust time
        socketio.emit('berkeley_log', {'message': 'Step 6-7: Clients adjust their local time with CAP values'})
        socketio.emit('berkeley_log', {'message': 'Berkeley synchronization completed successfully!'})
    
    thread = Thread(target=simulate_berkeley)
    thread.daemon = True
    thread.start()

@socketio.on('stop_berkeley_demo')
def handle_stop_berkeley():
    global berkeley_active
    berkeley_active = False
    socketio.emit('berkeley_log', {'message': 'Berkeley Algorithm demonstration stopped'})

@socketio.on('start_ricart_demo')
def handle_start_ricart():
    global ricart_active, ricart_processes
    ricart_active = True
    
    # Reset process states
    for process_id in ricart_processes:
        ricart_processes[process_id] = {
            'state': 'RELEASED', 
            'clock': 0, 
            'request_timestamp': float('inf')
        }
    
    socketio.emit('ricart_log', {'message': 'Ricart-Agrawala demonstration started'})
    socketio.emit('ricart_status_update', {
        'processes': [
            {'id': pid, 'state': data['state'], 'clock': data['clock']} 
            for pid, data in ricart_processes.items()
        ]
    })
    
    # Simulate Ricart-Agrawala algorithm
    def simulate_ricart():
        import time
        time.sleep(2)
        
        # Teacher requests critical section
        ricart_processes['T']['state'] = 'WANTED'
        ricart_processes['T']['clock'] = 1
        ricart_processes['T']['request_timestamp'] = 1
        
        socketio.emit('ricart_log', {'message': 'Step 1-2: Teacher requests CS, broadcasts REQUEST(1, T)'})
        socketio.emit('ricart_status_update', {
            'processes': [
                {'id': pid, 'state': data['state'], 'clock': data['clock']} 
                for pid, data in ricart_processes.items()
            ]
        })
        time.sleep(2)
        
        # Other processes respond
        ricart_processes['S1']['clock'] = 2
        ricart_processes['S2']['clock'] = 2
        socketio.emit('ricart_log', {'message': 'Step 3-4: S1 and S2 receive request, send OK (not wanting CS)'})
        time.sleep(2)
        
        # Teacher enters CS
        ricart_processes['T']['state'] = 'HELD'
        ricart_processes['T']['clock'] = 3
        socketio.emit('ricart_log', {'message': 'Step 5-6: Teacher receives all OKs, enters Critical Section'})
        socketio.emit('ricart_status_update', {
            'processes': [
                {'id': pid, 'state': data['state'], 'clock': data['clock']} 
                for pid, data in ricart_processes.items()
            ]
        })
        time.sleep(3)
        
        # Student 1 requests CS while Teacher is in CS
        ricart_processes['S1']['state'] = 'WANTED'
        ricart_processes['S1']['clock'] = 4
        ricart_processes['S1']['request_timestamp'] = 4
        socketio.emit('ricart_log', {'message': 'Student 1 requests CS, but Teacher is still in CS (request deferred)'})
        socketio.emit('ricart_status_update', {
            'processes': [
                {'id': pid, 'state': data['state'], 'clock': data['clock']} 
                for pid, data in ricart_processes.items()
            ]
        })
        time.sleep(2)
        
        # Teacher releases CS
        ricart_processes['T']['state'] = 'RELEASED'
        ricart_processes['T']['clock'] = 5
        ricart_processes['T']['request_timestamp'] = float('inf')
        socketio.emit('ricart_log', {'message': 'Step 7: Teacher releases CS, sends OK to deferred requests'})
        time.sleep(1)
        
        # Student 1 enters CS
        ricart_processes['S1']['state'] = 'HELD'
        ricart_processes['S1']['clock'] = 6
        socketio.emit('ricart_log', {'message': 'Student 1 receives OK, enters Critical Section'})
        socketio.emit('ricart_status_update', {
            'processes': [
                {'id': pid, 'state': data['state'], 'clock': data['clock']} 
                for pid, data in ricart_processes.items()
            ]
        })
        time.sleep(3)
        
        # Student 1 releases CS
        ricart_processes['S1']['state'] = 'RELEASED'
        ricart_processes['S1']['clock'] = 7
        ricart_processes['S1']['request_timestamp'] = float('inf')
        socketio.emit('ricart_log', {'message': 'Student 1 releases CS. Ricart-Agrawala demonstration completed!'})
        socketio.emit('ricart_status_update', {
            'processes': [
                {'id': pid, 'state': data['state'], 'clock': data['clock']} 
                for pid, data in ricart_processes.items()
            ]
        })
    
    thread = Thread(target=simulate_ricart)
    thread.daemon = True
    thread.start()

@socketio.on('stop_ricart_demo')
def handle_stop_ricart():
    global ricart_active
    ricart_active = False
    socketio.emit('ricart_log', {'message': 'Ricart-Agrawala demonstration stopped'})

# --- MAIN EXECUTION BLOCK ---
if __name__ == '__main__':
    processor_server = ProcessorServer()
    print(f"Website running on http://127.0.0.1:{os.environ.get('PORT', 8080)}")
    socketio.run(app, host='127.0.0.1', port=int(os.environ.get('PORT', 8080)), debug=False, allow_unsafe_werkzeug=True)
