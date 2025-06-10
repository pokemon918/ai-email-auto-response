from __future__ import print_function
import os.path
import base64
import email
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage

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
from langdetect import detect
import httpx

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
        
        # Create OpenAI client with explicit configuration
        self.openai_client = OpenAI(
            api_key=api_key,
            http_client=httpx.Client(
                timeout=httpx.Timeout(30.0),
                verify=True
            )
        )
        self.blocked_senders = {
            "fastbookads@gmail.com",
            "fastamzads@gmail.com",
            "help@aweber.com",
            "advertise-noreply@global.metamail.com",
            "auth@pipedrive.com",
            "team@publishdrive.com",
            "info@mail.zapier.com",
            "notifications@calendly.com",
            "notification@facebookmail.com",
            "productupdates@send.calendly.com",
            "info@fastbookads.com",
            "update@global.metamail.com"
        }
        self.blocked_patterns = ["noreply", "no-reply"]
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
        """Recursively extract the body text from message payload."""
        def get_text_from_parts(parts):
            for part in parts:
                if part.get('mimeType') == 'text/plain' and 'data' in part.get('body', {}):
                    return base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='replace')
                elif part.get('mimeType', '').startswith('multipart/'):
                    # Recursively search in subparts
                    result = get_text_from_parts(part.get('parts', []))
                    if result:
                        return result
            # Fallback: try to get text/html if no text/plain found
            for part in parts:
                if part.get('mimeType') == 'text/html' and 'data' in part.get('body', {}):
                    return base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='replace')
            return ""
        
        if 'parts' in payload:
            return get_text_from_parts(payload['parts'])
        else:
            if payload.get('mimeType') == 'text/plain' and 'data' in payload.get('body', {}):
                return base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='replace')
            elif payload.get('mimeType') == 'text/html' and 'data' in payload.get('body', {}):
                return base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='replace')
        return ""
    
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
            
            cleaned_lines.append(line)
        
        return '\n'.join(cleaned_lines).strip()
    
    def generate_ai_response(self, message: Dict, tone: str) -> str:
        """Generate AI response using OpenAI, in the same language as the incoming email"""
        try:
            # Get the full conversation history
            conversation_history = self.get_thread_history(message['thread_id'])
            print("Conversation history: ",conversation_history)
            # Detect language from the latest message or the whole thread
            try:
                detected_lang = detect(message['body'])
            except Exception:
                detected_lang = 'it'  # fallback
            if detected_lang.startswith('en'):
                lang_instruction = "Reply in English."
            else:
                lang_instruction = "Rispondi in italiano."
            print(detected_lang)
            with open('message_data.txt', 'r', encoding='utf-8') as f:
                examples = f.read()

            # Create a prompt for the AI
            prompt = f"""
            You are the dedicated Email Specialist for Fast Book Ads (FBA‑Agent) and you reply from either fastbookads@gmail.com or info@fastbookads.com using correct language , english or italian.
            Use this tone: {tone}
            Must use this language for the response: {lang_instruction}, don't mix english and italian.
            Conversation history:
            {conversation_history}
            Context_Messages:
            {examples}

            1. DATA SOURCE & TRUTHFULNESS
                • The only authoritative source of facts is the CONTEXT_MESSAGES.  
                • Never fabricate information. If a detail is missing, ask a concise clarification question or explain the steps to acquire it.
            2. CONVERSATION AWARENESS
                • Always analyse the entire conversation (all CONTEXT_MESSAGES), not just the last email.  
                • Track open action items and reference earlier promises or attachments.  
                • Use the language that dominates the thread; default to Italian if the balance is equal.
            3. BRAND VOICE & TONE
                Mirror the style of previous Fast Book Ads outbound emails you detect in CONTEXT_MESSAGES:
                • Level of formality: moderately formal yet approachable  
                • Greetings: Only use Ciao + {{Name}} , Hi + {{Name}},or Hello + {{Name}} for greeting at only first response.
                  And then use “Clear + {{Name}}”,"Chiaro + {{Name}}", “Okay perfect + {{Name}},”,"Ok perfetto + {{Name}}" for greeting, not use Hi or Hello or Ciao.,  
                • Closings: "Grazie!" for Italian / "Best regards, for english
                • Sentences: 15‑25 words, active voice.  
                • Use succinct paragraphs; bullet‑points for lists and some emojis, not use bold format.
                • Keep language consistent—never mix Italian and English in the same paragraph.  
                • Friendly, solution‑oriented, and technically precise.
            4. STRUCTURE OF EVERY REPLY
                • Greeting  
                • One‑sentence recap of the request  
                • Numbered answers or steps (include code snippets or links as needed)  
                • Next action / offer of further help  
            5. FORMATTING RULES
                • Plaintext/Markdown only (no HTML).  
                • Line length ≤ 80 characters.  
                • Numbered lists for procedures; dashes for simple lists.  
                • Embed inline code with back‑ticks.
            6. IMPORTANT RULES
                • Don't use name from the tone
                • Don't write name or [Your Name] at the end of the message and write like this.
                • Never use "Thank you for reaching out", "thank you", "I appreciate your email", "I appreciate your message", "I appreciate your reaching out", "I appreciate your contacting us" expression or similar expressions of gratitude except for the end of the email.
                  Only use Hi + sender's name,or Hello + sender's name or Ciao + sender's name for greeting at first chat.
                  And then use “Clear + sender's name,”,"Chiara + sender's name", “Okay perfect + sender's name,”,"Ok perfetto + sender's name" for greeting, not use Hi or Hello or Ciao.
                  Don't use both, use only one.
                • Consider conversation history to generate the response.
                • Don't write signature at the end of email.
                • Use correct language for the response include greeting and closing.
            OUTPUT
            Return only the finished email reply as a string, following the guidance above.
            """

            # Call OpenAI API   
            response = self.openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": "You are the dedicated Email Specialist for Fast Book Ads (FBA‑Agent) and you reply from either fastbookads@gmail.com or info@fastbookads.com."},
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
            # subject = original_message['subject']
            # if not subject.lower().startswith('re:'):
            #     subject = f"Re: {subject}"
            
            # Create the root message as 'related'
            message = MIMEMultipart('related')
            message['to'] = sender_email
            # message['subject'] = subject
            
            # Alternative part for HTML (and optionally plain text)
            alternative_part = MIMEMultipart('alternative')
            
            # Prepare the AI response as HTML
            ai_response_html = ai_response.replace('\n', '<br>')
            html_body = f"""
            <div style='font-family: Arial; font-size: 14px; color: #222;'>
                {ai_response_html}
                <br><br>
                <div style='font-size: 14px; font-family: Arial; color: #222;'>   
                    Noemi
                </div>
                <div style='font-size: 14px; font-style: italic; font-family: Arial; color: #222;'>
                    Customer Success Assistant<br>
                    fastbookads.com
                </div>
                <img src='cid:signature' style='width:120px; max-width:100%; height:auto; margin-top:5px; display:block;'>
            </div>
            """
            alternative_part.attach(MIMEText(html_body, 'html'))
            
            # Attach the alternative part to the main message
            message.attach(alternative_part)
            
            # Attach the signature image
            signature_path = os.path.join(os.path.dirname(__file__), 'signature.jpg')
            if os.path.exists(signature_path):
                with open(signature_path, 'rb') as img_file:
                    signature_img = MIMEImage(img_file.read())
                    signature_img.add_header('Content-ID', '<signature>')
                    signature_img.add_header('Content-Disposition', 'inline', filename='signature.jpg')
                    message.attach(signature_img)
            else:
                print("⚠️ Warning: signature.jpg not found in the directory")
            
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
            # print(f"   Subject: {subject}")
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
            sender_email = self.extract_email_address(msg['sender'])
            if self.is_blocked_sender(sender_email):
                print(f"⏩ Skipping auto-reply for blocked sender: {sender_email}")
                continue
            # print(f"From: {msg['sender']}")
            # print(f"Subject: {msg['subject']}")
            # print(f"Date: {msg['date']}")
            # print(f"Body Preview: {msg['body'][:100]}...")
            # print(f"Message ID: {msg['id']}")
            
            # Generate AI response
            tone = extract_tone_from_examples("message_data.txt", self.openai_client)
            print("🤖 Generating AI response...")
            ai_response = self.generate_ai_response(msg, tone)
            
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
                time.sleep(interval_minutes * 10)
                
        except KeyboardInterrupt:
            print("\n🛑 Monitoring stopped by user")
        except Exception as error:
            print(f"❌ Error during monitoring: {error}")

    def is_blocked_sender(self, sender_email: str) -> bool:
        email_lower = sender_email.lower()
        if email_lower in self.blocked_senders:
            return True
        for pattern in self.blocked_patterns:
            if pattern in email_lower:
                return True
        return False

    def get_thread_history(self, thread_id):
        """Fetch all messages in a thread and build a conversation history string."""
        try:
            thread = self.service.users().threads().get(userId='me', id=thread_id, format='full').execute()
            messages = thread.get('messages', [])
            history = []
            print(len(messages))
            for msg in messages:
                headers = msg['payload'].get('headers', [])
                sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown Sender')
                date = next((h['value'] for h in headers if h['name'] == 'Date'), 'Unknown Date')
                subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
                body = self.extract_message_body(msg['payload'])
                # Optionally clean the body
                print("Body----------- "+body)
                clean_body = self.clean_email_body(body)
                history.append(f"From: {sender}\nDate: {date}\nSubject: {subject}\nMessage:\n{clean_body}\n")
            return "\n---\n".join(history)
        except Exception as error:
            print(f"❌ Error fetching thread history: {error}")
            return ""

def extract_tone_from_examples(file_path, openai_client):
    with open(file_path, 'r', encoding='utf-8') as f:
        examples = f.read()
    prompt = (
       """Analyze the writing style and tone of the person who is writing from the "fastbookads@gmail.com" email address in the provided email chat history. Pay attention to:
        1. Level of formality
        2. Greeting and closing styles
        3. Sentence structure and length
        4. Use of punctuation and formatting
        5. Vocabulary choices and any recurring phrases
        6. Level of directness/indirectness
        7. Use of questions vs. statements
        8. Emotional tone (friendly, professional, casual, etc.)
        9. Any distinctive writing patterns or quirks"""
        f"{examples}\n\nTone description:"
    )
    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are an expert at analyzing writing tone."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=30,
        temperature=0.2
    )
    return response.choices[0].message.content.strip()

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

