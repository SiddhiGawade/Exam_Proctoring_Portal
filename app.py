# app.py
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
from datetime import datetime, timedelta
import time
import random
from threading import Thread, Lock, Timer
import logging

# --- SETUP ---
# 1. Initialize the Flask App and SocketIO
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key!' 
socketio = SocketIO(app)

# 2. Silence the terminal logs from Flask's default server (Werkzeug)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# --- TASK 2 & 3: CHEATING DETECTION DATA ---
student_dataset = {
    '24102A2001': 'SIDDHI GAWADE', '24102A2002': 'SANDEEP MAJUMDAR',
    '24102A2003': 'CHIRAG CHAUDHARI', '24102A2004': 'ANUSHKA UNDE',
    '24102A2005': 'SHAZIYA SHAIKH', '24102A2006': 'TAMANNA SHAIKH',
    '24102A2007': 'TANSHIQ KULKARNI', '24102A2008': 'ARAV MAHIND',
    '24102A2009': 'PREM DESHMUKH', '24102A2010': 'ISHA BANSAL',
}

# This function resets the students' data to their initial state.
def reset_student_data():
    return {
        roll_no: {'name': name, 'marks': 100, 'cheating_count': 0, 'exam_terminated': False}
        for roll_no, name in student_dataset.items()
    }

students_data = reset_student_data()

# --- TASK 4: TIME SYNCHRONIZATION DATA ---
task4_data = {
    "client_cvs": {},
    "expected_clients": {"Student", "Teacher"}
}

# --- NEW: EXAM SUBMISSION DATA & LOCK ---
StatusDB = {}
SubmissionDB = {}
exam_active = False
exam_timer = None
cheating_thread = None
lock = Lock()

# --- FIX: Update the auto_submit_all function to include the student's name ---
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
                    "name": student_dataset.get(sid, "Unknown")  # Add this line to fetch the name
                }
                StatusDB[sid] = True
        exam_active = False
    
    log_msg = "Exam ended. All pending submissions have been auto-submitted."
    socketio.emit('processor_log', {'log': log_msg})
    socketio.emit('exam_status', {'status': 'ended'})
    socketio.emit('submission_update', SubmissionDB)

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

@app.route('/task4')
def task4_view():
    return render_template('task4.html')

# --- TASK 4: API ENDPOINTS FOR TIME SYNC ---
@app.route("/broadcast_time", methods=["GET"])
def broadcast_time():
    server_time = datetime.now().strftime("%H:%M:%S")
    log_msg = f"[Task 4] Server Time Broadcasted: {server_time}"
    socketio.emit('processor_log', {'log': log_msg})
    return jsonify({"server_time": server_time})

@app.route("/send_cv", methods=["POST"])
def receive_cv():
    data = request.json
    name, cv = data["name"], data["cv"]
    task4_data["client_cvs"][name] = cv
    log_msg = f"[Task 4] Received CV from {name}: {cv:.2f} sec"
    socketio.emit('processor_log', {'log': log_msg})
    return jsonify({"status": "CV received"})

@app.route("/calculate_adjustments", methods=["GET"])
def calculate_adjustments():
    if task4_data["expected_clients"].issubset(task4_data["client_cvs"].keys()):
        avg_cv = sum(task4_data["client_cvs"].values()) / len(task4_data["client_cvs"])
        log_msg = f"[Task 4] Average CV calculated: {avg_cv:.2f} sec"
        socketio.emit('processor_log', {'log': log_msg})
        
        cap_values = {name: avg_cv - cv for name, cv in task4_data["client_cvs"].items()}
        log_msg = f"[Task 4] CAPs calculated: {cap_values}"
        socketio.emit('processor_log', {'log': log_msg})
        
        return jsonify({"cap": cap_values})
    else:
        remaining = task4_data["expected_clients"] - set(task4_data["client_cvs"].keys())
        return jsonify({"status": f"Waiting for {len(remaining)} more clients..."})

@app.route("/reset_task4", methods=["POST"])
def reset_task4():
    task4_data["client_cvs"].clear()
    log_msg = "[Task 4] State has been reset."
    socketio.emit('processor_log', {'log': log_msg})
    return jsonify({"status": "Task 4 reset successfully"})

# --- NEW: EXAM SUBMISSION API ENDPOINTS ---
@app.route("/start_exam", methods=["POST"])
def start_exam():
    global exam_active, StatusDB, SubmissionDB, exam_timer, cheating_thread, students_data
    
    # Reset students_data on exam start
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

    # Start cheating detection thread only when exam starts
    if cheating_thread is None or not cheating_thread.is_alive():
        cheating_thread = Thread(target=cheating_simulation)
        cheating_thread.daemon = True
        cheating_thread.start()

    log_msg = f"Exam started for {len(students)} students. Duration: {duration} seconds."
    socketio.emit('processor_log', {'log': log_msg})
    socketio.emit('exam_status', {'status': 'started', 'duration': duration, 'students': students})
    socketio.emit('submission_status_update', StatusDB)
    
    return jsonify({"msg": "Exam started"})

@app.route("/manual_submit", methods=["POST"])
def manual_submit():
    data = request.json
    sid = data["student_id"]
    answers = data["answers"]

    with lock:
        if not exam_active:
            return jsonify({"error": "Exam not active"}), 400
        if sid not in StatusDB:
            return jsonify({"error": "Unknown student"}), 400
        if StatusDB[sid]:
            return jsonify({"error": "Already submitted"}), 400

        # Calculate marks based on number of answers (simple example)
        marks = len(answers)
        SubmissionDB[sid] = {
            "answers": answers,
            "auto": False,
            "marks": marks,
            "submitted_at": datetime.now().isoformat(),
            "name": student_dataset.get(sid, "Unknown")
        }
        StatusDB[sid] = True

    log_msg = f"Student {sid} submitted exam manually. Marks: {marks}"
    socketio.emit('processor_log', {'log': log_msg})
    socketio.emit('student_notification', {'message': f"Your exam has been submitted successfully.", 'type': 'success', 'timestamp': time.strftime('%H:%M:%S')})
    socketio.emit('submission_update', SubmissionDB)
    socketio.emit('submission_status_update', StatusDB)
    
    return jsonify({"msg": "Submitted successfully"})

# --- BACKGROUND THREAD FOR CHEATING SIMULATION ---
def cheating_simulation():
    socketio.sleep(3)
    socketio.emit('processor_log', {'log': 'Cheating simulation background thread started.'})
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

# --- SOCKET.IO CONNECTION ---
@socketio.on('connect')
def handle_connect():
    socketio.emit('processor_log', {'log': 'A new client connected to the website.'})
    socketio.emit('marks_update', list(students_data.items()))
    socketio.emit('submission_status_update', StatusDB)
    socketio.emit('submission_update', SubmissionDB)

# --- MAIN EXECUTION BLOCK ---
if __name__ == '__main__':
    print("Website running on http://127.0.0.1:5000")
    socketio.run(app, host='127.0.0.1', port=5000, debug=False, allow_unsafe_werkzeug=True)