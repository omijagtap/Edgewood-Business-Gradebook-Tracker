import os
import io
import time
import uuid
import threading
import builtins
import requests
import pandas as pd
import concurrent.futures
from flask import Flask, render_template, request, send_file, jsonify
from dotenv import load_dotenv

# Load env variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'edgewood_secret_key_default')

# Global state for live updates
task_progress = {}
task_results = {}
current_task_id = None

# Intercept print to stream live updates natively
original_print = builtins.print

def custom_print(*args, **kwargs):
    global current_task_id
    text = " ".join(str(a) for a in args)
    if current_task_id and text.strip():
        clean_text = text.strip()
        # Clean up prints for the UI
        if "Fetching" in clean_text:
            clean_text = f"Connecting to Canvas: {clean_text}"
            
        # Ignore empty lines or simple separators
        if not set(clean_text).issubset({'-', '=', ' '}):
            task_progress[current_task_id] = clean_text
            
    original_print(*args, **kwargs)

builtins.print = custom_print

# --- Canvas API Constants ---
API_URL = os.getenv("CANVAS_API_URL", "https://edgewood.instructure.com/api/v1")
ACCESS_TOKEN = os.getenv("CANVAS_ACCESS_TOKEN", "")
HEADERS = {"Authorization": f"Bearer {ACCESS_TOKEN}"}

def get_all_pages(url, headers, params=None):
    results = []
    while url:
        try:
            response = requests.get(url, headers=headers, params=params, timeout=20)
            if response.status_code != 200:
                break
            data = response.json()
            if isinstance(data, list):
                results.extend(data)
            else:
                results.append(data)
            url = response.links.get('next', {}).get('url')
            params = None  # Params are already in the 'next' url
        except Exception as e:
            print(f"Exception during pagination: {e}")
            break
    return results

def resolve_course_details(search_term):
    """
    Finds course ID and Name by searching across accounts or direct ID lookup.
    """
    print(f"Searching for course '{search_term}'...")
    
    # 1. If it's a numeric ID, try direct fetch
    if search_term.isdigit():
        resp = requests.get(f"{API_URL}/courses/{search_term}", headers=HEADERS)
        if resp.status_code == 200:
            c = resp.json()
            return c.get('id'), c.get('name')

    # 2. Search in user's active courses
    courses = get_all_pages(f"{API_URL}/courses", HEADERS, params={"per_page": 100, "state[]": "available"})
    seen_ids = set()
    matched = []
    for c in courses:
        name = str(c.get('name', '')).lower()
        code = str(c.get('course_code', '')).lower()
        cid = str(c.get('id', ''))
        term = search_term.lower()
        if term == cid or term in name or term in code:
            if cid not in seen_ids:
                seen_ids.add(cid)
                matched.append(c)

    # 3. Search in specific accounts if no match found
    if not matched:
        try:
            accounts = get_all_pages(f"{API_URL}/accounts", HEADERS)
            acc_ids = [acc.get('id') for acc in accounts if acc.get('id')]
        except Exception as e:
            print(f"Exception fetching accounts: {e}")
            acc_ids = []

        # Fallback to hardcoded accounts (including 177)
        for fallback_id in [141, 177, 105, 1, 13]:
            if fallback_id not in acc_ids:
                acc_ids.append(fallback_id)

        for acc_id in acc_ids:
            try:
                acc_courses = get_all_pages(f"{API_URL}/accounts/{acc_id}/courses", HEADERS, params={"search_term": search_term, "per_page": 100})
                for c in acc_courses:
                    cid = str(c.get('id', ''))
                    if cid not in seen_ids:
                        seen_ids.add(cid)
                        matched.append(c)
            except Exception as e:
                # Silently handle permission errors for specific accounts
                pass

    if not matched:
        return None, None

    # In web app, instead of input() prompt on ambiguity, we select the first match and print warning
    if len(matched) > 1:
        print(f"[WARNING] Multiple courses match '{search_term}'. Selecting first match:")
        for i, c in enumerate(matched):
            print(f"  [{i+1}] {c.get('name')} (ID: {c.get('id')})")
        c = matched[0]
        print(f"Selected: {c.get('name')} (ID: {c.get('id')})")
        return c.get('id'), c.get('name')

    c = matched[0]
    return c.get('id'), c.get('name')

def process_course_report(course_input):
    course_id, course_name = resolve_course_details(course_input)
    if not course_id:
        print(f"[ERROR] Could not find course '{course_input}'. Skipping...")
        return None
        
    print(f"Processing: {course_name} (ID: {course_id})")
    
    # 1. Fetch Students, Assignments and Submissions concurrently
    print(f"Fetching student lists, assignments, and submissions concurrently for {course_name}...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        enrollments_future = executor.submit(
            get_all_pages,
            f"{API_URL}/courses/{course_id}/enrollments", 
            HEADERS, 
            params={"type[]": "StudentEnrollment", "state[]": "active", "per_page": 100}
        )
        assignments_future = executor.submit(
            get_all_pages,
            f"{API_URL}/courses/{course_id}/assignments", 
            HEADERS, 
            params={"per_page": 100}
        )
        submissions_future = executor.submit(
            get_all_pages,
            f"{API_URL}/courses/{course_id}/students/submissions", 
            HEADERS, 
            params={"student_ids[]": "all", "per_page": 100}
        )
        
        enrollments = enrollments_future.result()
        assignments_list = assignments_future.result()
        submissions = submissions_future.result()
        
    if not enrollments:
        print(f"No active students found for course {course_id}. Skipping...")
        return None

    students = {}
    for e in enrollments:
        u = e.get("user", {})
        uid = e.get("user_id")
        if uid not in students:
            # Extract score and grade
            grades_info = e.get("grades", {})
            current_score = grades_info.get("current_score")
            current_grade = grades_info.get("current_grade")
            
            if current_score is not None:
                total_percentage = f"{round(float(current_score), 2)}%"
            else:
                total_percentage = "0.0%"
                
            if current_grade is not None:
                total_grade = current_grade
            else:
                total_grade = "F"

            students[uid] = {
                "Student ID": u.get("sis_user_id") or e.get("sis_user_id") or "N/A",
                "Learner Name": u.get("name", "N/A"),
                "Official Email": u.get("email") or u.get("login_id") or "N/A",
                "Total Percentage": total_percentage,
                "Total Grade": total_grade
            }

    # Fallback to resolve student IDs if they are missing (N/A)
    has_missing_sis = any(s["Student ID"] == "N/A" for s in students.values())
    if has_missing_sis:
        print("SIS User IDs (Student IDs) are missing. Attempting fallback token to resolve them...")
        fallback_token = os.getenv("CANVAS_FALLBACK_TOKEN", "")
        fallback_headers = {"Authorization": f"Bearer {fallback_token}"} if fallback_token else {}
        fallback_enrollments = get_all_pages(
            f"{API_URL}/courses/{course_id}/enrollments", 
            fallback_headers, 
            params={"type[]": "StudentEnrollment", "state[]": "active", "per_page": 100}
        )
        for fe in fallback_enrollments:
            fuid = fe.get("user_id")
            fsis = fe.get("sis_user_id") or fe.get("user", {}).get("sis_user_id")
            if fuid in students:
                if fsis:
                    students[fuid]["Student ID"] = fsis
                # Also resolve Total Percentage and Total Grade from fallback if they were defaulted
                if students[fuid]["Total Percentage"] == "0.0%" and students[fuid]["Total Grade"] == "F":
                    grades_info = fe.get("grades", {})
                    current_score = grades_info.get("current_score")
                    current_grade = grades_info.get("current_grade")
                    if current_score is not None:
                        students[fuid]["Total Percentage"] = f"{round(float(current_score), 2)}%"
                    if current_grade is not None:
                        students[fuid]["Total Grade"] = current_grade

    # Process Assignments
    assignments = {}
    for a in assignments_list:
        due_at = a.get("due_at")
        if due_at:
            due_date = due_at.split("T")[0]
            title = f"{a.get('name')} ({a.get('id')}) (Due: {due_date})"
        else:
            title = f"{a.get('name')} ({a.get('id')}) (No Due Date)"
        assignments[a.get("id")] = title

    # Prepare Data for Pivoting
    data = []
    for uid, s_info in students.items():
        for aid, a_title in assignments.items():
            data.append({
                "uid": uid,
                "Student ID": s_info["Student ID"],
                "Learner Name": s_info["Learner Name"],
                "Official Email": s_info["Official Email"],
                "Total Percentage": s_info["Total Percentage"],
                "Total Grade": s_info["Total Grade"],
                "Assignment": a_title,
                "Status": "No Submission"
            })

    submission_map = {}
    for sub in submissions:
        uid = sub.get("user_id")
        aid = sub.get("assignment_id")
        if uid not in students or aid not in assignments: continue
            
        s_info = students[uid]
        a_title = assignments[aid]
        
        score = sub.get("score")
        workflow_state = sub.get("workflow_state")
        grade_matches = sub.get("grade_matches_current_submission", True)
        attempt = sub.get("attempt") or 0
        
        status = "No Submission"

        # Check if it is ungraded or resubmitted
        is_needs_grading = (workflow_state in ("submitted", "pending_review")) or (grade_matches is False)

        if is_needs_grading:
            submitted_at = sub.get("submitted_at")
            cached_due_date = sub.get("cached_due_date")
            date_str = submitted_at.split("T")[0] if submitted_at else "No Date"
            
            is_late = sub.get("late") or sub.get("late_policy_status") == "late"
            if not is_late and submitted_at and cached_due_date:
                if submitted_at > cached_due_date:
                    is_late = True

            if (attempt > 1) or (not grade_matches):
                status = f"Resubmitted - Need Grading ({date_str})"
            elif is_late:
                status = f"Late Submission - Need Grading ({date_str})"
            else:
                status = f"Needs Grading ({date_str})"
        elif workflow_state == "graded":
            status = str(round(float(score), 2)) if score is not None else "0"
        elif workflow_state == "unsubmitted":
            status = "No Submission"
        
        submission_map[(uid, a_title)] = status

    for item in data:
        key = (item["uid"], item["Assignment"])
        if key in submission_map:
            item["Status"] = submission_map[key]

    # Create DataFrame and Pivot
    df = pd.DataFrame(data)
    if df.empty:
        print(f"[ERROR] No gradebook entries to compile for {course_name}. Skipping...")
        return None

    pivot_df = df.pivot_table(
        index=["uid", "Student ID", "Learner Name", "Official Email", "Total Percentage", "Total Grade"], 
        columns="Assignment", 
        values="Status", 
        aggfunc='first'
    ).reset_index()
    pivot_df = pivot_df.drop(columns=["uid"])

    def get_needs_grading(row):
        needs = [
            col for col in pivot_df.columns 
            if any(status in str(row[col]) for status in ["Needs Grading", "Late Submission", "Resubmitted"])
        ]
        return ", ".join(needs) if needs else ""

    pivot_df["Assignments to Grade"] = pivot_df.apply(get_needs_grading, axis=1)
    student_cols = ["Learner Name", "Student ID", "Official Email", "Assignments to Grade"]
    end_cols = ["Total Percentage", "Total Grade"]
    assign_cols = sorted([c for c in pivot_df.columns if c not in (student_cols + end_cols)])
    pivot_df = pivot_df[student_cols + assign_cols + end_cols]
    pivot_df = pivot_df.sort_values(by="Assignments to Grade", key=lambda x: x == "", ascending=True)

    print(f"[SUCCESS] Compiled {len(students)} students and {len(assignments)} assignments for {course_name}.")
    needs_count = len(pivot_df[pivot_df["Assignments to Grade"] != ""])
    if needs_count > 0:
        print(f"[INFO] {needs_count} students need grading in {course_name}.")
        
    return {
        "course_id": course_id,
        "course_name": course_name,
        "df": pivot_df,
        "students_count": len(students),
        "assignments_count": len(assignments)
    }

def run_audit(task_id, course_codes_input):
    global current_task_id
    current_task_id = task_id
    task_progress[task_id] = "Initializing Canvas API Connection..."
    
    try:
        if "," in course_codes_input:
            course_list = [c.strip() for c in course_codes_input.split(",") if c.strip()]
        else:
            course_list = [c.strip() for c in course_codes_input.split() if c.strip()]

        if not course_list:
            task_progress[task_id] = "ERROR: No valid Course codes or IDs found in input."
            return

        # Prepare in-memory Excel file
        output = io.BytesIO()
        processed_count = 0

        # We'll run the course reports concurrently using thread pool
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(course_list), 5)) as executor:
            futures = {executor.submit(process_course_report, course_input): course_input for course_input in course_list}
            for future in concurrent.futures.as_completed(futures):
                course_input = futures[future]
                try:
                    res = future.result()
                    if res:
                        results.append(res)
                except Exception as exc:
                    print(f"[ERROR] Course {course_input} generated an exception: {exc}")

        if not results:
            task_progress[task_id] = "ERROR: No courses were successfully processed."
            return

        task_progress[task_id] = "Writing sheets into Excel workbook..."
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            for res in results:
                course_id = res["course_id"]
                course_name = res["course_name"]
                pivot_df = res["df"]

                # Clean sheet name to fit Excel limits
                sheet_name = "".join(x for x in course_name if x.isalnum() or x in " -_").strip()
                for c in [':', '\\', '/', '?', '*', '[', ']']:
                    sheet_name = sheet_name.replace(c, '')
                sheet_name = sheet_name[:30]
                if not sheet_name:
                    sheet_name = f"Course_{course_id}"

                pivot_df.to_excel(writer, sheet_name=sheet_name, index=False)
                processed_count += 1

        task_progress[task_id] = "Finalizing Output File..."
        excel_data = output.getvalue()
        output.close()
        
        safe_first_name = "".join(x for x in course_list[0] if x.isalnum() or x in " -_").strip()
        task_results[task_id] = {
            "file": io.BytesIO(excel_data),
            "filename": f"Gradebook_Report_{safe_first_name}.xlsx"
        }
        
        time.sleep(0.5)
        task_progress[task_id] = "COMPLETE"
    except Exception as e:
        task_progress[task_id] = f"ERROR: {str(e)}"
    finally:
        current_task_id = None

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start', methods=['POST'])
def start_audit():
    data = request.json
    course_codes_input = data.get('course_codes', '')
    if not course_codes_input:
        return jsonify({"error": "Please enter at least one course code or ID."}), 400
        
    task_id = str(uuid.uuid4())
    task_progress[task_id] = "Warming up Gradebook Report Engine..."
    
    # Run the report generation in a background thread
    thread = threading.Thread(target=run_audit, args=(task_id, course_codes_input))
    thread.start()
    
    return jsonify({"task_id": task_id})

@app.route('/status/<task_id>')
def status(task_id):
    state = task_progress.get(task_id, "Unknown Task")
    return jsonify({"status": state})

@app.route('/download/<task_id>')
def download(task_id):
    if task_id in task_results:
        res = task_results[task_id]
        return send_file(
            res["file"],
            as_attachment=True,
            download_name=res["filename"],
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    return "File not found or expired.", 404

if __name__ == '__main__':
    app.run(debug=True, port=5001, threaded=True)
