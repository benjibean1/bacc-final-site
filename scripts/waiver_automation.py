"""
BACC Waiver Automation Script - Corrected
==========================================
Fixes:
1. Google eSignature creates a PDF copy with timestamp in Pending folder
2. We keep the PDF, delete the Google Doc from Completed
3. DB should store the PDF file_id (not the Google Doc)

Author: Clay (Hermes)
Last updated: 2026-08-04
"""
import json
import os
import re
import subprocess
import tempfile
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
TEMPLATE_DOC_ID = '1Yz8TGj7lbcsPuc4_RTvUofHU_-DtHg2VMNeODPMD6SQ'  # BACC 2026-27 Waiver Documents (Final)
ARCHIVE_LABEL = 'Label_1'  # Waiver-Done Gmail label

# LXC SSH config
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
    """Write SQL to a Python script, SCP to LXC, execute, return results as list of dicts."""
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
    """Execute a write query on LXC via SSH."""
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
    """Check and create needed columns in waived_documents table."""
    columns = [
        'status', 'sent_date', 'signed_date', 'reminder_count'
    ]
    
    rows = deploy_and_run_lxc_script("PRAGMA table_info(waived_documents)")
    existing = {r['name'] for r in rows}
    
    for col in columns:
        if col not in existing:
            if col == 'reminder_count':
                dtype = 'INTEGER DEFAULT 0'
            else:
                dtype = 'TEXT'
            run_lxc_write(f"ALTER TABLE waived_documents ADD COLUMN {col} {dtype}")
            print(f"  Created column: waived_documents.{col}")

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
# PHASE 2: PROCESS COMPLETED WAIVERS
# ──────────────────────────────────────────────

def process_completed_emails(gmail, drive):
    print("\n=== PHASE 2: PROCESSING COMPLETED WAIVERS ===")
    
    try:
        # Google sends "eSigned Document Ready: [DocName] - timestamp"
        results = gmail.users().messages().list(
            userId='me',
            q='in:inbox subject:"eSigned Document Ready"',
            labelIds=['INBOX']
        ).execute()
        
        messages = results.get('messages', [])
        if not messages:
            print("  No completion emails found.")
            return 0
        
        # Get all pending waivers from DB
        pending_waivers = deploy_and_run_lxc_script(
            "SELECT athlete_id, drive_file_id, guardian_email FROM waived_documents WHERE status = 'pending'"
        )
        file_map = {row['drive_file_id']: row for row in pending_waivers}
        
        completed_count = 0
        for msg_info in messages:
            msg = gmail.users().messages().get(
                userId='me', id=msg_info['id'],
                format='metadata', metadataHeaders=['Subject']
            ).execute()
            
            subject = next(
                (h['value'] for h in msg['payload']['headers'] if h['name'].lower() == 'subject'), ''
            )
            
            if 'eSigned Document Ready:' not in subject:
                continue
            
            # Extract doc name from: eSigned Document Ready: "Waiver_Name_20260804 - 8/3/26, 8:29 PM"
            match = re.search(r'eSigned Document Ready: "([^"]+)"', subject)
            if not match:
                continue
            
            full_doc_name = match.group(1)
            # Strip timestamp suffix
            doc_name = re.sub(r' - \d+/\d+/\d+,\s*\d+:\d+\s*[AP]M$', '', full_doc_name)
            
            # Search for the PDF in Pending folder (Google saves PDF here with timestamp)
            pending_results = drive.files().list(
                q=f"'{PENDING_FOLDER_ID}' in parents and name = '{full_doc_name}' and mimeType = 'application/pdf' and trashed = false",
                fields='files(id, name)'
            ).execute()
            
            files = pending_results.get('files', [])
            if not files:
                print(f"  No PDF found for: {doc_name}")
                continue
            
            pdf_id = files[0]['id']
            print(f"  Found PDF: {files[0]['name']} (ID: {pdf_id})")
            
            # Find matching DB record by searching for any pending record
            # that matches this athlete (we match by athlete name pattern)
            db_record = None
            for fid, record in file_map.items():
                # Check if this record's file_id matches any pending file
                pending_check = drive.files().list(
                    q=f"'{PENDING_FOLDER_ID}' in parents and id = '{fid}' and trashed = false",
                    fields='files(id)'
                ).execute()
                if pending_check.get('files'):
                    # This file still exists in Pending - check if it matches this athlete
                    # We'll just take the first match since there should only be one pending per athlete
                    db_record = record
                    break
            
            if not db_record:
                # Fallback: try to match by athlete name in doc name
                parts = doc_name.replace('Waiver_', '').split('_')
                if len(parts) >= 2:
                    athlete_pattern = f"{parts[0]}_{parts[1]}"
                    for fid, record in file_map.items():
                        if athlete_pattern.lower() in fid.lower():
                            db_record = record
                            break
            
            if not db_record:
                print(f"  No DB record found for: {doc_name}")
                continue
            
            # Move PDF to Completed folder
            drive.files().update(
                fileId=pdf_id,
                addParents=COMPLETED_FOLDER_ID,
                removeParents=PENDING_FOLDER_ID
            ).execute()
            print(f"  Moved PDF to Completed folder")
            
            # Find and delete the Google Doc from Pending or Completed
            google_doc_results = drive.files().list(
                q=f"(\'{PENDING_FOLDER_ID}\' in parents OR \'{COMPLETED_FOLDER_ID}\' in parents) and name = \'{doc_name}\' and mimeType contains \'google-apps.document\' and trashed = false",
                fields='files(id, name)'
            ).execute()
            
            google_docs = google_doc_results.get('files', [])
            for gd in google_docs:
                drive.files().delete(fileId=gd['id']).execute()
                print(f"  Deleted Google Doc: {gd['name']}")
            
            # Update DB with PDF file_id
            run_lxc_write(f"""
                UPDATE waived_documents 
                SET status = 'completed', signed_date = '{datetime.now().strftime('%Y-%m-%d')}',
                    drive_file_id = '{pdf_id}'
                WHERE athlete_id = {db_record['athlete_id']}
            """)
            print(f"  DB UPDATED: athlete_id={db_record['athlete_id']}, status=completed, PDF_id={pdf_id}")
            
            # Archive email
            gmail.users().messages().modify(
                userId='me', id=msg['id'],
                body={'removeLabelIds': ['INBOX'], 'addLabelIds': [ARCHIVE_LABEL]}
            ).execute()
            print(f"  Archived email")
            
            completed_count += 1
        
        print(f"  Total completed waivers processed: {completed_count}")
        return completed_count
        
    except Exception as e:
        print(f"  ERROR processing emails: {e}")
        return 0

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
    
    # Upload to Drive
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
        gmail = build('gmail', 'v1', credentials=creds)
        
        # Ensure schema
        ensure_schema()
        
        send_new_waivers(drive)
        process_completed_emails(gmail, drive)
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
