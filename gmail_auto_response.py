from __future__ import print_function
import os.path
import base64
import email
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from dotenv import load_dotenv
import time
import datetime
from typing import List, Dict
from openai import OpenAI
import re

load_dotenv()

# Gmail API scopes - Updated to include drafts
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly', 
          'https://www.googleapis.com/auth/gmail.compose']

class GmailAutoReply:
    def __init__(self):
        self.service = None
        self.last_check_time = None
        self.processed_message_ids = set()
        
        # Initialize OpenAI
        api_key = os.getenv('OPENAI_API_KEY')
        if not api_key:
            raise ValueError("OPENAI_API_KEY not found in environment variables")
        self.openai_client = OpenAI(
            api_key=api_key,
        )
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
                message_id = next((h['value'] for h in headers if h['name'] == 'Message-ID'), '')
                
                # Get message body
                body = self.extract_message_body(msg_detail['payload'])
                
                message_info = {
                    'id': msg_id,
                    'subject': subject,
                    'sender': sender,
                    'date': date,
                    'body': body,
                    'thread_id': msg_detail['threadId'],
                    'message_id': message_id
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
    
    def clean_email_body(self, body: str) -> str:
        """Clean email body by removing signatures, quoted text, etc."""
        # Remove common email signatures and quoted text
        lines = body.split('\n')
        cleaned_lines = []
        
        for line in lines:
            # Skip lines that look like quoted text
            if line.strip().startswith('>'):
                break
            # Skip lines that look like forwarded messages
            if 'From:' in line and 'To:' in line:
                break
            # Skip common signature indicators
            if line.strip() in ['--', '___', 'Best regards', 'Sincerely', 'Thanks']:
                break
            cleaned_lines.append(line)
        
        return '\n'.join(cleaned_lines).strip()
    
    def generate_ai_response(self, message: Dict) -> str:
        """Generate AI response using OpenAI"""
        try:
            # Clean the message body
            clean_body = self.clean_email_body(message['body'])
            
            # Create a prompt for the AI
            prompt = f"""
            You are a professional email assistant. Generate a polite and helpful response to the following email:
            
            From: {message['sender']}
            Subject: {message['subject']}
            Message: {clean_body}
            
            Please write a professional response that:
            1. Acknowledges the sender's message
            2. Provides helpful information if possible
            3. Is concise and polite
            4. Uses appropriate business email tone
            
            Response:
            """
            
            # Call OpenAI API
            response = self.openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": "You are a professional email assistant that writes polite, helpful, and concise email responses."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=500,
                temperature=0.7
            )
            
            ai_response = response.choices[0].message.content.strip()
            return ai_response
            
        except Exception as error:
            print(f"❌ Error generating AI response: {error}")
            return "Thank you for your email. I have received your message and will get back to you soon."
    
    def extract_email_address(self, sender: str) -> str:
        """Extract email address from sender string"""
        # Use regex to extract email from "Name <email@domain.com>" format
        match = re.search(r'<(.+?)>', sender)
        if match:
            return match.group(1)
        # If no angle brackets, assume the whole string is the email
        return sender.strip()
    
    def create_draft_reply(self, original_message: Dict, ai_response: str):
        """Create a draft reply using the AI-generated response"""
        try:
            # Extract sender email
            sender_email = self.extract_email_address(original_message['sender'])
            
            # Create reply subject
            subject = original_message['subject']
            if not subject.lower().startswith('re:'):
                subject = f"Re: {subject}"
            
            # Create the email message
            message = MIMEMultipart()
            message['to'] = sender_email
            message['subject'] = subject
            
            # Add In-Reply-To and References headers for proper threading
            if original_message['message_id']:
                message['In-Reply-To'] = original_message['message_id']
                message['References'] = original_message['message_id']
            
            # Add the AI-generated response as the body
            message.attach(MIMEText(ai_response, 'plain'))
            
            # Convert to raw message
            raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
            
            # Create draft
            draft_body = {
                'message': {
                    'raw': raw_message,
                    'threadId': original_message['thread_id']
                }
            }
            
            # Save as draft
            draft = self.service.users().drafts().create(
                userId='me', body=draft_body).execute()
            
            print(f"✅ Draft created for message from {sender_email}")
            print(f"   Subject: {subject}")
            print(f"   Draft ID: {draft['id']}")
            return draft
            
        except Exception as error:
            print(f"❌ Error creating draft: {error}")
            return None
    
    def process_new_messages(self, messages: List[Dict]):
        """Process new messages and generate AI replies"""
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
            
            # Generate AI response
            print("🤖 Generating AI response...")
            ai_response = self.generate_ai_response(msg)
            
            # Create draft reply
            print("📝 Creating draft reply...")
            draft = self.create_draft_reply(msg, ai_response)
            
            if draft:
                print(f"✅ Draft saved successfully!")
                print(f"AI Response Preview: {ai_response[:100]}...")
            
            print("-" * 50)
    
    def start_monitoring(self, interval_minutes: int = 1):
        """Start monitoring Gmail inbox for new messages"""
        print(f"🚀 Starting Gmail auto-reply monitoring (checking every {interval_minutes} minute(s))")
        print("📝 AI responses will be saved as drafts for review")
        print("Press Ctrl+C to stop monitoring")
        
        # Set initial check time to now
        self.last_check_time = datetime.datetime.now()
        
        try:
            while True:
                current_time = datetime.datetime.now()
                print(f"\n🔍 Checking for new messages at {current_time.strftime('%Y-%m-%d %H:%M:%S')}")
                
                # Get new messages
                new_messages = self.get_new_messages()
                
                # Process new messages and generate AI replies
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
    """Main function to run the Gmail auto-reply system"""
    auto_reply = GmailAutoReply()
    
    try:
        # Authenticate with Gmail
        auto_reply.authenticate()
        
        # Start monitoring (check every 2 minutes to avoid rate limits)
        auto_reply.start_monitoring(interval_minutes=1)
        
    except Exception as error:
        print(f"❌ Error: {error}")
        print("\nMake sure you have:")
        print("1. Created a Google Cloud Project")
        print("2. Enabled the Gmail API")
        print("3. Downloaded credentials.json file")
        print("4. Set OPENAI_API_KEY in your .env file")
        print("5. Installed required packages: pip install -r requirements.txt")

if __name__ == '__main__':
    main()

