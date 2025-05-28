from __future__ import print_function
import os.path
import base64

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from dotenv import load_dotenv
import time
import datetime
from typing import List, Dict

load_dotenv()

# Gmail API scopes
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly', 
          'https://www.googleapis.com/auth/gmail.send']

class GmailMonitor:
    def __init__(self):
        self.service = None
        self.last_check_time = None
        self.processed_message_ids = set()
        
    def authenticate(self):
        """Authenticate with Gmail API"""
        creds = None
        # The file token.json stores the user's access and refresh tokens.
        if os.path.exists('token.json'):
            creds = Credentials.from_authorized_user_file('token.json', SCOPES)
        
        # If there are no (valid) credentials available, let the user log in.
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    'credentials.json', SCOPES)
                creds = flow.run_local_server(port=0)
            # Save the credentials for the next run
            with open('token.json', 'w') as token:
                token.write(creds.to_json())
        
        self.service = build('gmail', 'v1', credentials=creds)
        print("✅ Successfully authenticated with Gmail API")
        
    def get_new_messages(self) -> List[Dict]:
        """Get new messages from inbox since last check"""
        try:
            # Build query to get unread messages
            query = 'is:unread in:inbox'
            
            # If we have a last check time, only get messages after that
            if self.last_check_time:
                # Convert to Gmail's date format
                date_str = self.last_check_time.strftime('%Y/%m/%d')
                query += f' after:{date_str}'
            
            # Call the Gmail API
            results = self.service.users().messages().list(
                userId='me', q=query, maxResults=50).execute()
            
            messages = results.get('messages', [])
            new_messages = []
            
            for message in messages:
                msg_id = message['id']
                
                # Skip if we've already processed this message
                if msg_id in self.processed_message_ids:
                    continue
                    
                # Get full message details
                msg_detail = self.service.users().messages().get(
                    userId='me', id=msg_id, format='full').execute()
                
                # Extract message info
                headers = msg_detail['payload'].get('headers', [])
                subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
                sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown Sender')
                date = next((h['value'] for h in headers if h['name'] == 'Date'), 'Unknown Date')
                
                # Get message body
                body = self.extract_message_body(msg_detail['payload'])
                
                message_info = {
                    'id': msg_id,
                    'subject': subject,
                    'sender': sender,
                    'date': date,
                    'body': body,
                    'thread_id': msg_detail['threadId']
                }
                
                new_messages.append(message_info)
                self.processed_message_ids.add(msg_id)
                
            return new_messages
            
        except Exception as error:
            print(f"❌ Error fetching messages: {error}")
            return []
    
    def extract_message_body(self, payload):
        """Extract the body text from message payload"""
        body = ""
        
        if 'parts' in payload:
            for part in payload['parts']:
                if part['mimeType'] == 'text/plain':
                    if 'data' in part['body']:
                        body = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8')
                        break
                elif part['mimeType'] == 'text/html' and not body:
                    if 'data' in part['body']:
                        body = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8')
        else:
            if payload['mimeType'] == 'text/plain':
                if 'data' in payload['body']:
                    body = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8')
        
        return body
    
    def process_new_messages(self, messages: List[Dict]):
        """Process new messages (you can customize this function)"""
        if not messages:
            return
            
        print(f"\n📧 Found {len(messages)} new message(s):")
        print("-" * 50)
        
        for msg in messages:
            print(f"From: {msg['sender']}")
            print(f"Subject: {msg['subject']}")
            print(f"Date: {msg['date']}")
            print(f"Body Preview: {msg['body'][:100]}...")
            print(f"Message ID: {msg['id']}")
            print("-" * 50)
            
            # Here you can add your custom processing logic
            # For example:
            # - Auto-reply to certain emails
            # - Forward emails to specific addresses
            # - Save attachments
            # - Trigger other automations
    
    def start_monitoring(self, interval_minutes: int = 1):
        """Start monitoring Gmail inbox for new messages"""
        print(f"🚀 Starting Gmail monitoring (checking every {interval_minutes} minute(s))")
        print("Press Ctrl+C to stop monitoring")
        
        # Set initial check time to now
        self.last_check_time = datetime.datetime.now()
        
        try:
            while True:
                current_time = datetime.datetime.now()
                print(f"\n🔍 Checking for new messages at {current_time.strftime('%Y-%m-%d %H:%M:%S')}")
                
                # Get new messages
                new_messages = self.get_new_messages()
                
                # Process new messages
                self.process_new_messages(new_messages)
                
                if not new_messages:
                    print("✅ No new messages found")
                
                # Update last check time
                self.last_check_time = current_time
                
                # Wait for the specified interval
                print(f"⏰ Waiting {interval_minutes} minute(s) until next check...")
                time.sleep(interval_minutes * 60)
                
        except KeyboardInterrupt:
            print("\n🛑 Monitoring stopped by user")
        except Exception as error:
            print(f"❌ Error during monitoring: {error}")

def main():
    """Main function to run the Gmail monitor"""
    monitor = GmailMonitor()
    
    try:
        # Authenticate with Gmail
        monitor.authenticate()
        
        # Start monitoring (check every 1 minute)
        monitor.start_monitoring(interval_minutes=1)
        
    except Exception as error:
        print(f"❌ Error: {error}")
        print("\nMake sure you have:")
        print("1. Created a Google Cloud Project")
        print("2. Enabled the Gmail API")
        print("3. Downloaded credentials.json file")
        print("4. Installed required packages: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client python-dotenv openai")

if __name__ == '__main__':
    main()

