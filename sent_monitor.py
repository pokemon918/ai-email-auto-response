import os
import pickle
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from pymongo import MongoClient
from datetime import datetime
import base64
from email.mime.text import MIMEText
import json
import dotenv
dotenv.load_dotenv()
# Gmail API scopes
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

class GmailMongoDB:
    def __init__(self, credentials_path='credentials.json', token_path='token.json', mongo_uri=os.getenv('MONGODB_URI')):
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.mongo_uri = mongo_uri
        self.gmail_service = None
        self.mongo_client = None
        self.db = None

    def authenticate_gmail(self):
        """Authenticate with Gmail API."""
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

        self.gmail_service = build('gmail', 'v1', credentials=creds)
        return self.gmail_service

    def connect_mongodb(self):
        """Connect to MongoDB."""
        self.mongo_client = MongoClient(self.mongo_uri)
        print(self.mongo_uri)
        self.db = self.mongo_client['email_history']
        return self.db

    def get_email_content(self, message):
        """Extract email content from Gmail message."""
        if 'payload' not in message:
            return None

        headers = message['payload']['headers']
        subject = next((header['value'] for header in headers if header['name'].lower() == 'subject'), 'No Subject')
        sender = next((header['value'] for header in headers if header['name'].lower() == 'from'), 'Unknown Sender')
        to = next((header['value'] for header in headers if header['name'].lower() == 'to'), 'Unknown Recipient')
        date = next((header['value'] for header in headers if header['name'].lower() == 'date'), None)

        # Get email body
        body = ''
        if 'parts' in message['payload']:
            for part in message['payload']['parts']:
                if part['mimeType'] == 'text/plain':
                    body = base64.urlsafe_b64decode(part['body']['data']).decode()
                    break
        elif 'body' in message['payload'] and 'data' in message['payload']['body']:
            body = base64.urlsafe_b64decode(message['payload']['body']['data']).decode()

        return {
            'message_id': message['id'],
            'thread_id': message['threadId'],
            'subject': subject,
            'sender': sender,
            'to': to,
            'date': date,
            'body': body,
            'snippet': message.get('snippet', ''),
            'labels': message.get('labelIds', []),
            'stored_at': datetime.utcnow()
        }

    def get_thread_messages(self, thread_id):
        """Fetch all messages in a thread."""
        if not self.gmail_service:
            self.authenticate_gmail()

        thread = self.gmail_service.users().threads().get(
            userId='me', id=thread_id).execute()
        
        messages = []
        for message in thread['messages']:
            email_data = self.get_email_content(message)
            if email_data:
                # Create a version of the message for thread context that includes body
                thread_message = {
                    'message_id': email_data['message_id'],
                    'thread_id': email_data['thread_id'],
                    'subject': email_data['subject'],
                    'sender': email_data['sender'],
                    'to': email_data['to'],
                    'date': email_data['date'],
                    'snippet': email_data['snippet'],
                    'body': email_data['body'],  # Include the body content
                    'labels': email_data['labels']  # Include labels as well
                }
                messages.append(thread_message)
        
        # Sort messages by date
        messages.sort(key=lambda x: x['date'] if x['date'] else '')
        return messages

    def fetch_and_store_emails(self, max_results=30):
        """Fetch emails from Gmail SENT and store them in MongoDB with thread messages included."""
        if not self.gmail_service:
            self.authenticate_gmail()
        if not self.db:
            self.connect_mongodb()
        print("Connected to MongoDB")

        # Get messages from Gmail inbox only
        results = self.gmail_service.users().messages().list(
            userId='me',
            labelIds=['SENT'],  # Only fetch messages from inbox
            maxResults=max_results
        ).execute()
        messages = results.get('messages', [])
        print(f"Found {len(messages)} emails in sent")

        # Collection for storing emails
        emails_collection = self.db['email_history']

        # Keep track of processed threads to avoid duplicates
        processed_threads = set()

        count = 0
        for message in messages:
            print(f"Processing email: {message['id']}"+"count: "+str(count))
            count += 1
            thread_id = message['threadId']
            
            # Skip if we've already processed this thread
            if thread_id in processed_threads:
                continue

            # Get all messages in the thread
            thread_messages = self.get_thread_messages(thread_id)
            
            if thread_messages:
                # Store each message with its thread context
                for msg in thread_messages:
                    # Get the full message details for the current message
                    full_message = self.gmail_service.users().messages().get(
                        userId='me', 
                        id=msg['message_id'],
                        format='full'  # Get full message details
                    ).execute()
                    email_data = self.get_email_content(full_message)
                    
                    if email_data:
                        # Add thread context without creating circular references
                        email_data['thread_context'] = {
                            'thread_id': thread_id,
                            'message_count': len(thread_messages),
                            'messages': thread_messages
                        }
                        # Only store if the message is in inbox
                        if 'SENT' in email_data['labels']:
                            emails_collection.update_one(
                                {'message_id': email_data['message_id']},
                                {'$set': email_data},
                                upsert=True
                            )
                
                processed_threads.add(thread_id)
                print(f"Stored thread {thread_id} with {len(thread_messages)} messages")

        return len(processed_threads)

    def close(self):
        """Close MongoDB connection."""
        if self.mongo_client:
            self.mongo_client.close()

def main():
    # Initialize the GmailMongoDB class
    gmail_mongo = GmailMongoDB()
    
    try:
        # Fetch and store emails
        num_emails = gmail_mongo.fetch_and_store_emails(max_results=10000)
        print(f"Successfully processed {num_emails} emails")
    except Exception as e:
        print(f"An error occurred: {str(e)}")
    finally:
        gmail_mongo.close()

if __name__ == '__main__':
    main()
