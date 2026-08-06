"""
BACC Waiver Automation Script - Gmail-based detection
======================================================
Uses Gmail API to detect completed eSignature waivers.
Looks for emails from esignature-noreply@google.com with 
subject "eSigned Document Ready: [DocName]"

Author: Clay (Hermes)
Last updated: 2026-08-06
"""
import json
import os
import re
import sys
import csv
import subprocess
import tempfile
import base64
from datetime import datetime
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
        return []
    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError:
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
    return result.stdout.strip()

def ensure_schema():
    columns = ['status', 'sent_date', 'signed_date', 'reminder_count']
    rows = deploy_and_run_lxc_script("PRAGMA table_info(waived_documents)")
    existing = {r['name'] for r in rows}
    for col in columns:
        if col not in existing:
            dtype = 'INTEGER DEFAULT 0' if col == 'reminder_count' else 'TEXT'
            run_lxc_write(f"ALTER TABLE waived_documents ADD COLUMN {col} {dtype}")
            print(f"  Created column: waived_documents.{col}")

def parse_doc_name_from_subject(subject):
    """Extract document name from Gmail subject.
    
    Formats:
    - eSignature request for "Waiver_Name_20260804 - 8/3/26, 8:29 PM"
    - eSigned Document Ready: "Waiver_Name_20260804 - 8/3/26, 8:29 PM"
    Returns: "Waiver_Name_20260804"
    """
    # Extract quoted document name (handles both formats)
    match = re.search(r'"([^"]+)"', subject)
    if match:
        doc_name_with_ts = match.group(1)
        # Strip timestamp suffix: " - M/D/YY, H:MM AM/PM"
        base_name = re.sub(r' - \d+/\d+/\d+,\s*\d+:\d+(?::\d+)?\s*[AP]M$', '', doc_name_with_ts)
        return base_name
    return None

def find_athlete_by_doc_name(doc_name, athletes):
    """Match doc name to athlete in DB.
    
    Doc names: Waiver_FirstLast_YYYYMMDD
    """
    match = re.match(r'Waiver_(.+)_(\d{8})', doc_name)
    if not match:
        return None
    
    athlete_part = match.group(1)
    parts = athlete_part.rsplit('_', 1)
    if len(parts) == 2:
        first_name, last_name = parts
        for rec in athletes:
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
# PHASE 2: DETECT COMPLETED WAIVERS (GMAIL-BASED)
# ──────────────────────────────────────────────

def parse_email_body(msg_data):
    """Extract text/plain body from Gmail message."""
    def get_body_text(payload):
        """Recursively find text/plain body."""
        if 'parts' not in payload:
            return None
        
        for part in payload['parts']:
            mime = part.get('mimeType', '')
            if mime == 'text/plain':
                body_data = part.get('body', {}).get('data', '')
                if body_data:
                    return base64.urlsafe_b64decode(body_data).decode('utf-8')
            if 'parts' in part:
                result = get_body_text(part)
                if result:
                    return result
        return None
    
    return get_body_text(msg_data['payload'])

def process_completed_gmail(gmail, drive):
    """Detect completed waivers by scanning Gmail for eSignature completion emails."""
    print("\n=== PHASE 2: DETECTING COMPLETED WAIVERS (GMAIL) ===")
    
    # Search for eSignature emails from the system (all labels)
    results = gmail.users().messages().list(
        userId='me',
        q='from:esignature-noreply@google.com'
    ).execute()
    
    messages = results.get('messages', [])
    print(f"  Found {len(messages)} completion emails in inbox")
    
    if not messages:
        print("  No new completions found.")
        return 0
    
    # Get all pending athletes from DB
    athletes = deploy_and_run_lxc_script("""
        SELECT a.id, a.first_name, a.last_name,
               wd.drive_file_id, wd.status
        FROM athletes a
        LEFT JOIN waived_documents wd ON wd.athlete_id = a.id AND wd.waiver_type = '2026-2027 Medical Consent'
        WHERE wd.status = 'pending'
    """)
    
    athlete_map = {f"{a['first_name']} {a['last_name']}": a for a in athletes}
    
    completed_count = 0
    
    for msg in messages:
        # Fetch full message
        msg_data = gmail.users().messages().get(
            userId='me',
            id=msg['id'],
            format='metadata',
            metadataHeaders=['Subject', 'From', 'Date']
        ).execute()
        
        headers = {h['name']: h['value'] for h in msg_data['payload']['headers']}
        subject = headers.get('Subject', '')
        
        # Extract document name from subject
        doc_name = parse_doc_name_from_subject(subject)
        if not doc_name:
            print(f"  WARNING: Could not parse document name from: {subject}")
            continue
        
        print(f"\n  Found completion: {subject}")
        print(f"    Parsed doc name: {doc_name}")
        
        # Find matching athlete
        athlete = find_athlete_by_doc_name(doc_name, athletes)
        if not athlete:
            print(f"  WARNING: No matching athlete found for {doc_name}")
            continue
        
        athlete_id = athlete['id']
        print(f"    Athlete ID: {athlete_id}")
        
        # Get full body to verify completion
        full_msg = gmail.users().messages().get(
            userId='me',
            id=msg['id'],
            format='full'
        ).execute()
        
        body = parse_email_body(full_msg)
        if body and 'completed' in body.lower():
            print(f"    Email body confirms: COMPLETED")
        else:
            print(f"    WARNING: Could not verify completion in body")
            continue
        
        # Find the PDF in Pending folder (Google creates it after signing)
        # List all PDFs in Pending and find the matching one
        pending_pdfs = drive.files().list(
            q=f"'{PENDING_FOLDER_ID}' in parents and mimeType='application/pdf' and trashed=false",
            fields='files(id, name)'
        ).execute()
        
        pdf_files = [f for f in pending_pdfs.get('files', []) if doc_name in f['name']]
        if not pdf_files:
            print(f"  WARNING: No PDF found in Pending for {doc_name}")
            continue
        
        pdf = pdf_files[0]
        print(f"    PDF found: {pdf['name']}")
        
        # Move PDF to Completed
        drive.files().update(
            fileId=pdf['id'],
            addParents=COMPLETED_FOLDER_ID,
            removeParents=PENDING_FOLDER_ID
        ).execute()
        print(f"    Moved PDF to Completed folder")
        
        # Delete the Google Doc from Pending
        google_doc = drive.files().list(
            q=f"'{PENDING_FOLDER_ID}' in parents and name = '{doc_name}' and mimeType contains 'google-apps.document'",
            fields='files(id, name)'
        ).execute()
        
        for gd in google_doc.get('files', []):
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
        
        # Archive email
        gmail.users().messages().modify(
            userId='me',
            id=msg['id'],
            body={'removeLabelIds': ['INBOX'], 'addLabelIds': [ARCHIVE_LABEL]}
        ).execute()
        print(f"    Email archived to Waiver-Done")
        
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
        gmail_api = build('gmail', 'v1', credentials=creds)
        
        ensure_schema()
        
        send_new_waivers(drive)
        process_completed_gmail(gmail_api, drive)
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
