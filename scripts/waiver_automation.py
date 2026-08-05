"""
BACC Waiver Automation Script - Drive-based detection
======================================================
Fixes:
1. Google doesn't reliably send completion emails to admin inbox
2. Instead, Google creates a PDF in Pending folder with timestamp suffix
3. We detect completed waivers by finding PDFs in Pending folder
4. Keep PDF, delete Google Doc, update DB

Author: Clay (Hermes)
Last updated: 2026-08-04
"""
import json
import os
import re
import sys
import csv
import subprocess
import tempfile
from datetime import datetime, timedelta
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload, MediaIoBaseDownload
from google.oauth2.credentials import Credentials

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────
TOKEN_PATH = '/opt/hermes-data/bacc/.hermes/google_token.json'
PENDING_FOLDER_ID = '1G8zF9A38qqvodinXTL3_uEHQn1reuFhx'
COMPLETED_FOLDER_ID = '1XBYOGjzrZPoxpfnRZY2Ute9aNKsfJpbX'
TEMPLATE_DOC_ID = '1Yz8TGj7lbcsPuc4_RTvUofHU_-DtHg2VMNeODPMD6SQ'
ARCHIVE_LABEL = 'Label_1'

SSH_KEY = '/root/.ssh/id_proxmox'
SSH_USER = 'root'
SSH_HOST = '192.168.100.66'

# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────

def get_credentials():
    with open(TOKEN_PATH, 'r') as f:
        creds_data = json.load(f)
    return Credentials(
        token=creds_data.get('token', ''),
        client_id=creds_data.get('client_id'),
        client_secret=creds_data.get('client_secret'),
        refresh_token=creds_data.get('refresh_token'),
        token_uri=creds_data.get('token_uri')
    )

def deploy_and_run_lxc_script(sql):
    script_content = f"""
import sqlite3
import json
conn = sqlite3.connect('/opt/bacc/backend/data/bacc.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute('''{sql}''')
rows = [dict(r) for r in cur.fetchall()]
conn.close()
print(json.dumps(rows))
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(script_content)
        temp_path = f.name
    remote_path = f'/tmp/bacc_waiver_{datetime.now().strftime("%Y%m%d_%H%M%S")}.py'
    scp_cmd = f"scp -i {SSH_KEY} -o StrictHostKeyChecking=no {temp_path} {SSH_USER}@{SSH_HOST}:{remote_path}"
    subprocess.run(scp_cmd, shell=True, capture_output=True, text=True)
    ssh_cmd = f"ssh -i {SSH_KEY} -o StrictHostKeyChecking=no {SSH_USER}@{SSH_HOST} 'python3 {remote_path} && rm {remote_path}'"
    result = subprocess.run(ssh_cmd, shell=True, capture_output=True, text=True, timeout=30)
    os.unlink(temp_path)
    if result.returncode != 0:
        print(f"  SSH ERROR: {result.stderr[:200]}")
        return []
    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        print(f"  JSON ERROR: {result.stdout[:200]}")
        return []

def run_lxc_write(sql):
    script_content = f"""
import sqlite3
conn = sqlite3.connect('/opt/bacc/backend/data/bacc.db')
cur = conn.cursor()
cur.execute('''{sql}''')
conn.commit()
print('OK')
conn.close()
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(script_content)
        temp_path = f.name
    remote_path = f'/tmp/bacc_waiver_{datetime.now().strftime("%Y%m%d_%H%M%S")}.py'
    scp_cmd = f"scp -i {SSH_KEY} -o StrictHostKeyChecking=no {temp_path} {SSH_USER}@{SSH_HOST}:{remote_path}"
    subprocess.run(scp_cmd, shell=True, capture_output=True, text=True)
    ssh_cmd = f"ssh -i {SSH_KEY} -o StrictHostKeyChecking=no {SSH_USER}@{SSH_HOST} 'python3 {remote_path} && rm {remote_path}'"
    result = subprocess.run(ssh_cmd, shell=True, capture_output=True, text=True, timeout=30)
    os.unlink(temp_path)
    return result.returncode == 0 and 'OK' in result.stdout

def ensure_schema():
    columns = ['status', 'sent_date', 'signed_date', 'reminder_count']
    rows = deploy_and_run_lxc_script("PRAGMA table_info(waived_documents)")
    existing = {r['name'] for r in rows}
    for col in columns:
        if col not in existing:
            dtype = 'INTEGER DEFAULT 0' if col == 'reminder_count' else 'TEXT'
            run_lxc_write(f"ALTER TABLE waived_documents ADD COLUMN {col} {dtype}")
            print(f"  Created column: waived_documents.{col}")

def parse_pdf_filename(filename):
    """Extract base doc name from PDF filename.
    
    PDF filenames have timestamp suffix: 'Waiver_Name_20260804 - 8/3/26, 1:36 PM'
    We strip the timestamp to get the base: 'Waiver_Name_20260804'
    """
    # Strip timestamp: " - M/D/YY, H:MM AM/PM" or " - M/D/YY, H:MM:SS AM/PM"
    return re.sub(r' - \d+/\d+/\d+,\s*\d+:\d+(?::\d+)?\s*[AP]M$', '', filename)

def find_athlete_by_doc_name(doc_name, db_records):
    """Match a doc name to a DB record by extracting athlete name.
    
    Doc names are formatted: Waiver_FirstLast_YYYYMMDD
    We match FirstLast to athlete first_name + last_name
    """
    # Extract athlete name from doc name
    match = re.match(r'Waiver_(.+)_(\d{8})', doc_name)
    if not match:
        return None
    
    athlete_part = match.group(1)
    parts = athlete_part.rsplit('_', 1)  # Split from right: ['First', 'Last']
    if len(parts) == 2:
        first_name, last_name = parts
        for rec in db_records:
            if rec['first_name'].lower() == first_name.lower() and rec['last_name'].lower() == last_name.lower():
                return rec
    return None

# ──────────────────────────────────────────────
# PHASE 1: SEND NEW WAIVERS
# ──────────────────────────────────────────────

def send_new_waivers(drive):
    print("\n=== PHASE 1: SENDING NEW WAIVERS ===")
    
    athletes = deploy_and_run_lxc_script("""
        SELECT a.id, a.first_name, a.last_name,
               g.email as guardian_email
        FROM athletes a
        LEFT JOIN guardians g ON g.athlete_id = a.id
        WHERE a.id NOT IN (SELECT athlete_id FROM waived_documents)
    """)
    
    if not athletes:
        print("  No new athletes to send waivers to.")
        return 0
    
    sent_count = 0
    for athlete in athletes:
        doc_name = f"Waiver_{athlete['first_name']}_{athlete['last_name']}_{datetime.now().strftime('%Y%m%d')}"
        
        try:
            copy_result = drive.files().copy(
                fileId=TEMPLATE_DOC_ID,
                body={'name': doc_name, 'parents': [PENDING_FOLDER_ID]}
            ).execute()
            
            doc_id = copy_result['id']
            
            run_lxc_write(f"""
                INSERT INTO waived_documents 
                (athlete_id, waiver_type, drive_file_id, guardian_email, status, sent_date, created_at)
                VALUES ({athlete['id']}, '2026-2027 Medical Consent', '{doc_id}', '{athlete['guardian_email']}', 'pending', '{datetime.now().strftime('%Y-%m-%d')}', '{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}')
            """)
            
            print(f"  SENT: {athlete['first_name']} {athlete['last_name']}")
            print(f"    Email: {athlete['guardian_email']}")
            print(f"    Doc: {doc_name} (ID: {doc_id})")
            sent_count += 1
            
        except Exception as e:
            print(f"  ERROR: {athlete['first_name']} {athlete['last_name']} — {e}")
    
    print(f"  Total new waivers sent: {sent_count}")
    return sent_count

# ──────────────────────────────────────────────
# PHASE 2: DETECT COMPLETED WAIVERS (DRIVE-BASED)
# ──────────────────────────────────────────────

def process_completed_drives(drive):
    """Detect completed waivers by scanning Pending folder for PDFs with timestamps."""
    print("\n=== PHASE 2: DETECTING COMPLETED WAIVERS ===")
    
    # Get all pending athletes from DB
    athletes = deploy_and_run_lxc_script("""
        SELECT a.id, a.first_name, a.last_name,
               wd.drive_file_id, wd.status
        FROM athletes a
        LEFT JOIN waived_documents wd ON wd.athlete_id = a.id AND wd.waiver_type = '2026-2027 Medical Consent'
        WHERE wd.status = 'pending'
    """)
    
    if not athletes:
        print("  No pending waivers found in DB.")
        return 0
    
    # Get athlete lookup (id -> first_last)
    athlete_map = {}
    for a in athletes:
        athlete_map[a['id']] = f"{a['first_name']}_{a['last_name']}"
    
    # Scan Pending folder for PDFs with timestamp suffix (completed = PDF created)
    pending_results = drive.files().list(
        q=f"'{PENDING_FOLDER_ID}' in parents and mimeType = 'application/pdf' and trashed = false",
        fields='files(id, name, modifiedTime)'
    ).execute()
    
    pdf_files = pending_results.get('files', [])
    print(f"  Found {len(pdf_files)} PDF(s) in Pending folder")
    
    # Track which athlete IDs we've already processed (avoid double-processing)
    processed_athlete_ids = set()
    completed_count = 0
    
    for pdf in pdf_files:
        pdf_name = pdf['name']
        
        # Check if this PDF looks like a completed waiver (has timestamp suffix)
        match = re.search(r' - \d+/\d+/\d+,\s*\d+:\d+(?::\d+)?\s*[AP]M$', pdf_name)
        if not match:
            print(f"  Skipping non-completion PDF: {pdf_name}")
            continue
        
        # Extract base doc name
        base_doc_name = parse_pdf_filename(pdf_name)
        print(f"\n  Found completed: {pdf_name}")
        print(f"    Base name: {base_doc_name}")
        
        # Find matching athlete in DB
        athlete = find_athlete_by_doc_name(base_doc_name, athletes)
        if not athlete:
            print(f"  WARNING: No matching athlete found for {base_doc_name}")
            continue
        
        athlete_id = athlete['id']
        print(f"    Athlete ID: {athlete_id}")
        
        # Skip if already processed
        if athlete_id in processed_athlete_ids:
            print(f"    Already processed, skipping")
            continue
        
        # Move PDF to Completed folder
        drive.files().update(
            fileId=pdf['id'],
            addParents=COMPLETED_FOLDER_ID,
            removeParents=PENDING_FOLDER_ID
        ).execute()
        print(f"    Moved PDF to Completed folder")
        
        # Delete the Google Doc from Pending or Completed
        google_doc_results = drive.files().list(
            q=f"(\'{PENDING_FOLDER_ID}\' in parents OR \'{COMPLETED_FOLDER_ID}\' in parents) and name = \'{base_doc_name}\' and mimeType contains \'google-apps.document\' and trashed = false",
            fields='files(id, name)'
        ).execute()
        
        for gd in google_doc_results.get('files', []):
            drive.files().delete(fileId=gd['id']).execute()
            print(f"    Deleted Google Doc: {gd['name']}")
        
        # Update DB
        run_lxc_write(f"""
            UPDATE waived_documents 
            SET status = 'completed', 
                signed_date = '{datetime.now().strftime('%Y-%m-%d')}',
                drive_file_id = '{pdf['id']}'
            WHERE athlete_id = {athlete_id}
        """)
        print(f"    DB UPDATED: status=completed, signed_date={datetime.now().strftime('%Y-%m-%d')}, PDF_id={pdf['id']}")
        
        processed_athlete_ids.add(athlete_id)
        completed_count += 1
    
    print(f"\n  Total completed waivers processed: {completed_count}")
    return completed_count

# ──────────────────────────────────────────────
# PHASE 3: STATUS REPORT
# ──────────────────────────────────────────────

def generate_status_report():
    print("\n=== PHASE 3: GENERATING STATUS REPORT ===")
    
    rows = deploy_and_run_lxc_script("""
        SELECT a.first_name, a.last_name,
               g.email as guardian_email, g.relationship,
               wd.status as waiver_status, wd.sent_date, wd.signed_date
        FROM athletes a
        LEFT JOIN guardians g ON g.athlete_id = a.id
        LEFT JOIN waived_documents wd ON wd.athlete_id = a.id 
            AND wd.waiver_type = '2026-2027 Medical Consent'
        ORDER BY a.last_name, a.first_name
    """)
    
    csv_data = [['Athlete', 'Guardian Email', 'Relationship', 'Waiver Status', 'Sent Date', 'Signed Date']]
    for row in rows:
        csv_data.append([
            f"{row['first_name']} {row['last_name']}",
            row.get('guardian_email') or '',
            row.get('relationship') or '',
            row.get('waiver_status') or 'Not Started',
            row.get('sent_date') or '',
            row.get('signed_date') or ''
        ])
    
    temp_csv = '/tmp/bacc_waiver_status.csv'
    with open(temp_csv, 'w', newline='') as f:
        csv.writer(f).writerows(csv_data)
    
    drive = build('drive', 'v3', credentials=get_credentials())
    drive.files().create(
        body={
            'name': f'Waiver_Status_{datetime.now().strftime("%Y%m%d")}.csv',
            'parents': [PENDING_FOLDER_ID],
            'mimeType': 'text/csv'
        },
        media_body=MediaFileUpload(temp_csv, mimetype='text/csv')
    ).execute()
    
    os.remove(temp_csv)
    print(f"  CSV uploaded to Drive: Waiver_Status_{datetime.now().strftime('%Y%m%d')}.csv")
    return True

# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def main():
    print("=" * 60)
    print("BACC WAIVER AUTOMATION")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    try:
        creds = get_credentials()
        drive = build('drive', 'v3', credentials=creds)
        
        ensure_schema()
        
        send_new_waivers(drive)
        process_completed_drives(drive)
        generate_status_report()
        
        print("\n" + "=" * 60)
        print("SUCCESS")
        print("=" * 60)
        
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == '__main__':
    main()
