#!/usr/bin/env python3
import sqlite3
import time
import json
import os
import datetime
import logging
import urllib.request
import urllib.error
import traceback
import sys

# --- Configuration ---
ENDPOINT = "http://192.168.4.44:5000/ingest"
DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")
STATE_FILE = os.path.expanduser("~/.imessage_relay_state.json")
QUEUE_FILE = os.path.expanduser("~/.imessage_relay_queue.jsonl")
POLL_INTERVAL = 5.0

# Setup logging
log_file_path = os.path.expanduser("~/Library/Logs/imessage_relay.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file_path, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

def get_db_connection():
    db_uri = f"file:{DB_PATH}?mode=ro"
    return sqlite3.connect(db_uri, uri=True)

def parse_mac_date(mac_date):
    if not mac_date:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()
    # macOS Core Data timestamp (nanoseconds or seconds since Jan 1, 2001)
    if mac_date > 10000000000000000:
        unix_ts = (mac_date / 1000000000) + 978307200
    else:
        unix_ts = mac_date + 978307200
    return datetime.datetime.fromtimestamp(unix_ts, datetime.timezone.utc).isoformat()

def get_chat_participants(cursor, chat_id):
    try:
        cursor.execute('''
            SELECT h.id 
            FROM chat_handle_join chj 
            JOIN handle h ON chj.handle_id = h.ROWID 
            WHERE chj.chat_id = ?
        ''', (chat_id,))
        return [row[0] for row in cursor.fetchall() if row[0]]
    except Exception as e:
        logging.error(f"SQL failed in get_chat_participants(chat_id={chat_id}): {e}")
        raise

def get_current_max_rowid(conn):
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT MAX(ROWID) FROM message")
        row = cursor.fetchone()
        return row[0] if row and row[0] else 0
    except Exception as e:
        logging.error(f"SQL failed in get_current_max_rowid: {e}")
        raise

def load_queue():
    queue = []
    if os.path.exists(QUEUE_FILE):
        try:
            with open(QUEUE_FILE, 'r') as f:
                for line in f:
                    if line.strip():
                        queue.append(json.loads(line))
        except Exception as e:
            logging.error(f"Error loading queue: {e}")
    return queue

def save_queue(queue):
    try:
        with open(QUEUE_FILE, 'w') as f:
            for item in queue:
                f.write(json.dumps(item) + '\n')
    except Exception as e:
        logging.error(f"Error saving queue: {e}")

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                data = json.load(f)
                return data.get("last_rowid", 0), set(data.get("processed_guids", []))
        except Exception as e:
            logging.error(f"Error loading state: {e}")
    return 0, set()

def save_state(last_rowid, processed_guids):
    # Keep processed guids bounded to prevent unbounded growth
    guids_list = list(processed_guids)[-10000:]
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump({
                "last_rowid": last_rowid,
                "processed_guids": guids_list
            }, f)
    except Exception as e:
        logging.error(f"Error saving state: {e}")

def extract_attributed_body(data):
    """Extract text from macOS NSKeyedArchiver / TypedStream format bytes."""
    if not data:
        return ""
    
    # Try using PyObjC Foundation if available (built into macOS system Python in some versions)
    try:
        from Foundation import NSData, NSKeyedUnarchiver
        ns_data = NSData.dataWithBytes_length_(data, len(data))
        unarchived = NSKeyedUnarchiver.unarchiveObjectWithData_(ns_data)
        if hasattr(unarchived, 'string'):
            return unarchived.string()
    except Exception:
        pass
    
    # Fallback heuristic parser
    try:
        decoded = data.decode('utf-8', errors='ignore')
        garbage = [
            "streamtyped", "NSMutableString", "NSString", "NSDictionary", "NSObject", 
            "NSMutableAttributedString", "NSAttributedString", "NSParagraphStyle", "NSNumber",
            "__kIMMessagePartAttributeName", "NSValue", "iIi", "@+", "*"
        ]
        for g in garbage:
            decoded = decoded.replace(g, "")
        cleaned = "".join(c for c in decoded if ord(c) >= 32 or c in '\n\t')
        return cleaned.strip()
    except Exception:
        return ""

def post_payload(payload):
    """Attempts to send the payload. Returns True on success, False on failure."""
    try:
        req = urllib.request.Request(
            ENDPOINT, 
            data=json.dumps(payload).encode('utf-8'), 
            headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.status in [200, 201, 202, 204]:
                return True
            else:
                logging.warning(f"Unexpected HTTP status {response.status} for {payload.get('event_id')}")
                return False
    except urllib.error.URLError as e:
        logging.warning(f"Network error while posting {payload.get('event_id')}: {e.reason}")
        return False
    except Exception as e:
        logging.warning(f"Unexpected error while posting {payload.get('event_id')}: {e}")
        return False

def process_messages():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        last_rowid, processed_guids = load_state()
        
        if last_rowid == 0:
            current_max = get_current_max_rowid(conn)
            logging.info(f"First run detected. Setting last_rowid to {current_max}")
            save_state(current_max, processed_guids)
            return

        query = '''
            SELECT 
                m.ROWID,
                m.guid,
                m.is_from_me,
                m.text,
                m.attributedBody,
                m.date,
                c.ROWID as chat_id,
                c.guid as chat_guid,
                h.id AS sender_handle
            FROM message m
            LEFT JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            LEFT JOIN chat c ON cmj.chat_id = c.ROWID
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.ROWID > ?
            ORDER BY m.ROWID ASC
        '''
        
        try:
            cursor.execute(query, (last_rowid,))
            rows = cursor.fetchall()
        except Exception as e:
            logging.error(f"SQL failed in main message query (last_rowid={last_rowid}): {e}")
            raise
        
        # Load existing queue
        queue = load_queue()
        new_items_count = 0

        for row in rows:
            (rowid, guid, is_from_me, text, attributed_body, date, chat_id, chat_guid, sender_handle) = row
            
            # Update rowid immediately to prevent re-querying this message
            last_rowid = rowid
            
            # Skip if already processed
            if guid in processed_guids:
                continue
                
            # Parse text
            msg_text = text
            if msg_text is None:
                msg_text = extract_attributed_body(attributed_body)
            if msg_text is None:
                msg_text = ""
                
            # Direction
            direction = "outgoing" if is_from_me == 1 else "incoming"
            
            # Participants
            participants = []
            if chat_id:
                participants = get_chat_participants(cursor, chat_id)
                
            # Sender & Recipients
            if direction == "outgoing":
                sender = "Me"
                recipients = participants
            else:
                sender = sender_handle if sender_handle else "Unknown"
                recipients = [p for p in participants if p != sender]
                if "Me" not in recipients:
                    recipients.append("Me")
                    
            # Ensure recipients is an array even if empty (should never happen)
            if not isinstance(recipients, list):
                recipients = list(recipients)
                
            # Conversation Type (Me + participants = total)
            # participants list usually contains all others in the chat
            total_people = len(participants) + 1 
            chat_type = "direct" if total_people <= 2 else "group"
            
            # Ensure message.guid is event_id, wait, yes, guid from sql is message.guid
            event_id = guid if guid else "unknown"
            
            payload = {
                "event_id": event_id,
                "platform": "macos",
                "service": "imessage",
                "direction": direction,
                "sender": sender,
                "recipients": recipients,
                "conversation": {
                    "id": chat_guid if chat_guid else "unknown",
                    "type": chat_type
                },
                "message": {
                    "text": msg_text
                },
                "message_at": parse_mac_date(date),
                "observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
            }
            
            queue.append(payload)
            processed_guids.add(event_id)
            new_items_count += 1
            logging.info(f"[{direction.upper()}] {sender} -> {recipients}: {msg_text}")
            logging.info(f"Saved new message to queue: {event_id}")
            
        # Process queue
        if queue:
            successful_posts = 0
            # Try to send items sequentially
            remaining_queue = []
            server_offline = False
            
            for item in queue:
                if not server_offline:
                    success = post_payload(item)
                    if success:
                        successful_posts += 1
                        logging.info(f"Successfully posted {item['event_id']}")
                    else:
                        logging.warning(f"Failed to post {item['event_id']}, keeping in queue. Pausing network posts.")
                        server_offline = True
                        remaining_queue.append(item)
                else:
                    # Keep remaining items in queue
                    remaining_queue.append(item)
                    
            if successful_posts > 0 or len(remaining_queue) != len(queue):
                save_queue(remaining_queue)
            elif new_items_count > 0:
                # Only re-save if we added new items and nothing succeeded
                save_queue(remaining_queue)
        
        # Always save state to update rowid
        save_state(last_rowid, processed_guids)

    except sqlite3.OperationalError as e:
        if "unable to open database file" in str(e) or "authorization denied" in str(e):
            logging.error("【重大エラー: フルディスクアクセス不足】Macのシステム設定 > プライバシーとセキュリティ > フルディスクアクセス にて、実際に動いている実行ファイルを追加してください。")
            logging.error(f"process pid={os.getpid()} executable={sys.executable} script={__file__}")
            time.sleep(10)
        else:
            logging.error(f"Database error: {e}")
            time.sleep(2)
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        logging.error(traceback.format_exc())
        time.sleep(2)
    finally:
        if conn:
            conn.close()

def main():
    if not os.path.exists(DB_PATH):
        logging.error(f"chat.db not found at {DB_PATH}")
        return

    logging.info("Starting robust iMessage Relay monitor...")
    while True:
        process_messages()
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
