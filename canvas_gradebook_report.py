import requests
import pandas as pd
import sys
import os
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

# --- Configuration ---
API_URL = os.getenv("CANVAS_API_URL", "https://edgewood.instructure.com/api/v1")
ACCESS_TOKEN = os.getenv("CANVAS_ACCESS_TOKEN", "")
HEADERS = {"Authorization": f"Bearer {ACCESS_TOKEN}"}

def get_all_pages(url, headers, params=None):
    results = []
    while url:
        try:
            response = requests.get(url, headers=headers, params=params, timeout=20)
            if response.status_code != 200:
                # Silently handle access issues or non-existent endpoints during search fallback
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
        for acc_id in [141, 105, 1, 13]:
            acc_courses = get_all_pages(f"{API_URL}/accounts/{acc_id}/courses", HEADERS, params={"search_term": search_term, "per_page": 100})
            for c in acc_courses:
                cid = str(c.get('id', ''))
                if cid not in seen_ids:
                    seen_ids.add(cid)
                    matched.append(c)

    if not matched:
        return None, None

    # RCA FIX: If multiple courses match, show list and ask user to confirm
    if len(matched) > 1:
        print(f"\n[WARNING] Multiple courses found matching '{search_term}':")
        for i, c in enumerate(matched):
            print(f"  [{i+1}] {c.get('name')} (ID: {c.get('id')})")
        choice = input(f"Enter number to select correct course (1-{len(matched)}): ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(matched):
                c = matched[idx]
                return c.get('id'), c.get('name')
        except ValueError:
            pass
        print("[ERROR] Invalid selection.")
        return None, None

    c = matched[0]
    return c.get('id'), c.get('name')

def generate_report():
    print("\n--- Canvas Gradebook Report Generator ---")
    print("You can enter multiple Course Names or IDs separated by commas (e.g., EDU-736, 1571, EDU-835)")
    course_inputs = input("Enter Course Names or IDs: ").strip()
    
    if not course_inputs:
        print("No course input provided.")
        return

    # Split by comma and clean whitespace
    course_list = [c.strip() for c in course_inputs.split(",")]

    for course_input in course_list:
        if not course_input: continue
        
        print(f"\n" + "="*50)
        course_id, course_name = resolve_course_details(course_input)
        if not course_id:
            print(f"[ERROR] Could not find course '{course_input}'. Skipping...")
            continue

        print(f"Processing: {course_name} (ID: {course_id})")
        
        # 1. Fetch Students (Active Enrollments)
        print("Fetching students...")
        enrollments = get_all_pages(
            f"{API_URL}/courses/{course_id}/enrollments", 
            HEADERS, 
            params={"type[]": "StudentEnrollment", "state[]": "active", "per_page": 100}
        )
        
        if not enrollments:
            print(f"No active students found for course {course_id}. Skipping...")
            continue

        students = {}
        for e in enrollments:
            u = e.get("user", {})
            uid = e.get("user_id")
            if uid not in students:
                students[uid] = {
                    "Student ID": u.get("sis_user_id") or e.get("sis_user_id") or "N/A",
                    "Learner Name": u.get("name", "N/A"),
                    "Official Email": u.get("email") or u.get("login_id") or "N/A"
                }

        # Fallback to resolve student IDs if they are missing (N/A) from the primary token's response
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
                if fuid in students and fsis:
                    students[fuid]["Student ID"] = fsis

        # 2. Fetch Assignments
        print("Fetching assignments...")
        assignments_list = get_all_pages(
            f"{API_URL}/courses/{course_id}/assignments", 
            HEADERS, 
            params={"per_page": 100}
        )
        
        assignments = {}
        for a in assignments_list:
            due_at = a.get("due_at")
            if due_at:
                due_date = due_at.split("T")[0]
                title = f"{a.get('name')} ({a.get('id')}) (Due: {due_date})"
            else:
                title = f"{a.get('name')} ({a.get('id')}) (No Due Date)"
            assignments[a.get("id")] = title

        # 3. Fetch Submissions
        print("Fetching submissions...")
        submissions = get_all_pages(
            f"{API_URL}/courses/{course_id}/students/submissions", 
            HEADERS, 
            params={"student_ids[]": "all", "per_page": 100}
        )

        # 4. Prepare Data for Pivoting
        data = []
        for uid, s_info in students.items():
            for aid, a_title in assignments.items():
                data.append({
                    "uid": uid,
                    "Student ID": s_info["Student ID"],
                    "Learner Name": s_info["Learner Name"],
                    "Official Email": s_info["Official Email"],
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

            # Check if it is ungraded or resubmitted (and thus needs grading)
            is_needs_grading = (workflow_state in ("submitted", "pending_review")) or (grade_matches is False)

            if is_needs_grading:
                submitted_at = sub.get("submitted_at")
                cached_due_date = sub.get("cached_due_date")
                date_str = submitted_at.split("T")[0] if submitted_at else "No Date"
                
                # Determine if late based on Canvas tag or by comparing submission timestamp to due date
                is_late = sub.get("late") or sub.get("late_policy_status") == "late"
                if not is_late and submitted_at and cached_due_date:
                    if submitted_at > cached_due_date:
                        is_late = True

                # 1. Check if it is a resubmission (Canvas tagging "resubmitted" or ungraded with multiple attempts)
                if (attempt > 1) or (not grade_matches):
                    status = f"Resubmitted - Need Grading ({date_str})"
                # 2. Check if it is submitted late
                elif is_late:
                    status = f"Late Submission - Need Grading ({date_str})"
                # 3. Otherwise standard needs grading
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

        # 5. Create DataFrame and Pivot
        df = pd.DataFrame(data)
        pivot_df = df.pivot_table(
            index=["uid", "Student ID", "Learner Name", "Official Email"], 
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
        assign_cols = sorted([c for c in pivot_df.columns if c not in student_cols])
        pivot_df = pivot_df[student_cols + assign_cols]
        pivot_df = pivot_df.sort_values(by="Assignments to Grade", key=lambda x: x == "", ascending=True)

        # 6. Save Main Combined Gradebook in the script's directory
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
        except NameError:
            script_dir = os.path.abspath(".")
        clean_name = "".join(x for x in course_name if x.isalnum() or x in " -_").strip()
        output_file = os.path.join(script_dir, f"Gradebook_{clean_name}_{course_id}.csv")
        
        pivot_df.to_csv(output_file, index=False, encoding="utf-8-sig")
        print(f"[SUCCESS] Saved to: {output_file}")
        
        needs_count = len(pivot_df[pivot_df["Assignments to Grade"] != ""])
        if needs_count > 0:
            print(f"[INFO] {needs_count} students need grading.")
        
        print(f"Total: {len(students)} students, {len(assignments)} assignments.")
    
    print("\n" + "="*50)
    print("ALL PROCESSING COMPLETE")

if __name__ == "__main__":
    generate_report()
