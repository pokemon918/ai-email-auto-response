import os.path
import base64
from email import policy
from email.parser import BytesParser
import re
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from bs4 import BeautifulSoup
import json
from tqdm import tqdm
from dotenv import load_dotenv # Import the dotenv library

from pinecone import Pinecone
from pinecone import ServerlessSpec

from openai import OpenAI
import re
from langdetect import detect
import httpx

load_dotenv()
PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY")

# Scope for read-only access
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

def get_gmail_service():
    """Authenticates with the Gmail API and returns the service object."""
    creds = None
    if os.path.exists("token_client.json"):
        creds = Credentials.from_authorized_user_file("token_client.json", SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists("credentials.json"):
                print("Error: credentials.json not found.")
                return None
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())
            
    return build("gmail", "v1", credentials=creds)
def get_message_body(payload):
    """
    A robust function to recursively search for the email body.
    Prioritizes 'text/plain', falls back to 'text/html', and handles nested parts.
    Returns the clean text content.
    """
    body = ""
    html_body = ""

    def search_parts(parts):
        nonlocal body, html_body
        for part in parts:
            # If we've already found the plain text body, we can stop.
            if body:
                return

            mime_type = part.get('mimeType')
            part_body = part.get('body')
            
            # Skip attachments
            if part.get('filename'):
                continue

            if mime_type == 'text/plain':
                if part_body and part_body.get('data'):
                    body = base64.urlsafe_b64decode(part_body['data']).decode('utf-8')
            elif mime_type == 'text/html':
                if part_body and part_body.get('data'):
                    html_body = base64.urlsafe_b64decode(part_body['data']).decode('utf-8')
            elif 'parts' in part:
                # This part is a container for other parts, search them recursively
                search_parts(part['parts'])

    if 'parts' in payload:
        search_parts(payload['parts'])
    elif 'body' in payload and 'data' in payload['body']:
        # For simple, non-multipart emails
        if payload['mimeType'] == 'text/plain':
            body = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8')
        elif payload['mimeType'] == 'text/html':
            html_body = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8')

    # If we found a plain text body, use it.
    if body:
        return body.strip()
    # Otherwise, fall back to the HTML body and clean it up.
    elif html_body:
        soup = BeautifulSoup(html_body, "html.parser")
        return soup.get_text(separator="\n").strip()
    
    # If no body is found at all
    return "No textual body found."
def clean_reply_body(body_text):
    """
    Removes quoted text from an email body.
    """
    # Pattern for "On [date], [person] wrote:"
    # This is a common header for replies.
    header_pattern = re.compile(r"On.*wrote:", re.IGNORECASE)
    match = header_pattern.search(body_text)
    if match:
        # Cut the body at the start of the quote header
        body_text = body_text[:match.start()]

    # Pattern for other separators like "--- Original Message ---"
    separator_pattern = re.compile(r"---.*Original Message.*---", re.IGNORECASE)
    match = separator_pattern.search(body_text)
    if match:
        body_text = body_text[:match.start()]

    # Split the body into lines and remove lines starting with '>'
    lines = body_text.split('\n')
    cleaned_lines = []
    for line in lines:
        # Also catches "> >" etc.
        if not line.strip().startswith('>'):
            cleaned_lines.append(line)
    
    # Join the lines back and trim whitespace
    return "\n".join(cleaned_lines).strip()
def parse_and_print_message(message_data, message_title):
    """Parses a single message object from the API and prints its details."""
    
    # The payload contains headers, body, etc.
    payload = message_data['payload']
    headers = payload['headers']
    
    # Extract headers
    subject = next((h['value'] for h in headers if h['name'].lower() == 'subject'), 'No Subject')
    sender = next((h['value'] for h in headers if h['name'].lower() == 'from'), 'N/A')
    date = next((h['value'] for h in headers if h['name'].lower() == 'date'), 'N/A')

    # print("="*60)
    # print(f"--- {message_title} ---")
    # print(f"Subject: {subject}")
    # print(f"From: {sender}")
    # print(f"Date: {date}")
    # print("="*60)

    # --- Parse the Message Body ---
    full_body = get_message_body(payload)
    
    # *** NOW, CLEAN THE BODY BEFORE PRINTING ***
    # We only apply cleaning if it's a reply message
    cleaned_body = full_body
    if message_title == "FIRST REPLY":
        cleaned_body = clean_reply_body(full_body)

    return cleaned_body
    # print("\nBody (Cleaned):")
    # print(cleaned_body)
    # print("\n")


def find_first_conversation_with_reply(service,pc,index):
    """Finds the first thread with 2+ messages and prints the first message and its reply."""
    try:
        # 1. Get the list of threads from the inbox
        print("Searching for a conversation with at least one reply...")
        response = service.users().threads().list(userId='me', labelIds=['INBOX'],maxResults=1000).execute()
        threads = response.get('threads', [])
        print(len(threads))
        if not threads:
            print("No threads found in the inbox.")
            return

        records=[]
        count=0

        # 2. Loop through threads to find one with more than one message
        for thread_info in threads:
            thread_id = thread_info['id']
            thread_data = service.users().threads().get(userId='me', id=thread_id).execute()
            
            messages_in_thread = thread_data.get('messages', [])

            if len(messages_in_thread) >= 2:
                # print(f"Found a suitable conversation (Thread ID: {thread_id}). Processing...\n")
                
                original_message = messages_in_thread[0]
                first_reply = messages_in_thread[1]

                # 3. Parse and print the original message and the reply
                original_message_body = parse_and_print_message(original_message, "ORIGINAL MESSAGE")
                first_reply_body = parse_and_print_message(first_reply, "FIRST REPLY")
                if(first_reply_body.find("Customer Success Assistant") != -1):
                    # print(original_message_body)
                    # print("\n--------------------------------")
                    # print(first_reply_body)
                    # print("\n--------------------------------")
                    count+=1
                    print(count)
                    embedding=pc.inference.embed(
                        model="llama-text-embed-v2",
                        inputs=[original_message_body],
                        parameters={"input_type": "passage", "truncate": "END"}
                    )
                    # print(embedding[0]['values'])
                    records.append({
                        "id":str(count),
                        "values": embedding[0]['values'],
                        "metadata":{
                            "original_message":original_message_body[:15000],
                            "reply_message": first_reply_body[:15000]
                        }
                    })
                    if(count%10==0):
                        print("Upserting....")
                        index.upsert(
                            vectors=records
                        )
                        records=[]

        print("Successfully upserting")
        
        return
                

        # This message will only show if the loop finishes without finding a suitable thread
        print("Could not find a conversation with a reply on the first page of your inbox.")

    except HttpError as error:
        print(f"An error occurred: {error}")

def vector_search(message,pc,index):
    embedding=pc.inference.embed(
        model="llama-text-embed-v2",
        inputs=[message],
        parameters={"input_type": "passage", "truncate": "END"}
    )
    vector=embedding[0]['values']

    response = index.query(
        top_k=1,
        vector=vector,
        include_values=False,
        include_metadata=True
    )

    return response.matches



if __name__ == "__main__":
    load_dotenv()

    PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY")
    pc = Pinecone(api_key=PINECONE_API_KEY)

    index_name = "email-auto-response"
    pc = Pinecone(api_key=PINECONE_API_KEY)

    if not pc.has_index(index_name):
        pc.create_index(
            name=index_name,
            vector_type="dense",
            dimension=1024,
            metric="dotproduct",
            spec=ServerlessSpec(
                cloud="aws",
                region="us-east-1"
            )
        )
    index = pc.Index(index_name)
    print(f"Connected to index '{index_name}'.")


    message="""Hello,my name is Anthon, I want to get your service"""

    reply_message=vector_search(message,pc,index)[0]['metadata']['reply_message']
    print(reply_message)

    try:
        detected_lang = detect(message)
    except Exception:
        detected_lang = 'it'  # fallback
    if detected_lang.startswith('en'):
        lang_instruction = "Reply in English."
    else:
        lang_instruction = "Rispondi in italiano."
    print(detected_lang)

    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key:
        raise ValueError("OPENAI_API_KEY not found in environment variables")
    
    # Create OpenAI client with explicit configuration
    openai_client = OpenAI(
        api_key=api_key,
        http_client=httpx.Client(
            timeout=httpx.Timeout(30.0),
            verify=True
        )
    )
    conversation_history=""

    # Create a prompt for the AI
    prompt = f"""
You are an email response automation assistant. Your task is to generate email responses that closely match the tone, style, and approach of a provided reference reply.

Instructions:
1. **Analyze the reference reply_message for:**
   - Tone (formal, casual, friendly, professional, etc.)
   - Writing style (concise, detailed, conversational, etc.)
   - Language patterns and vocabulary choices
   - Level of formality
   - Emotional undertone (enthusiastic, neutral, empathetic, etc.)

2. **Generate a response that:**
   - Mirrors the same tone and style as the reply_message
   - Uses same sentence structure and language patterns
   - But use the same language of original message
   - Maintains consistent formality level
   - Feels natural and authentic in the established voice

3. **Ensure the response:**
   - Is contextually relevant to the original email
   - Maintains the same level of detail as the reference
   - Uses similar greeting and closing styles
   - Follows the same communication approach

CRITICAL RULES - FOLLOW EXACTLY:

1. LANGUAGE CONSISTENCY
• Response language: {lang_instruction}
• NEVER mix languages in the same email
• If language is Italian: ALL text must be Italian (greeting, body, closing)
• If language is English: ALL text must be English (greeting, body, closing)

2. NAME HANDLING
• Extract sender's name from the conversation history only
• If no clear name found, use generic greeting without name
• NEVER use placeholder names like [Name] or {{Name}}
• NEVER use names from the reference reply_message

FORMATTING:
• Plain text only
• Natural paragraph breaks
• Keep sentences conversational length
• No bullet points unless listing specific steps
• Never use names or sign at the end of message
• No signature block


Reference reply_message: {reply_message}
Original email to respond to: {message}
Conversation history: {conversation_history}


Generate a response that someone reading both messages would recognize as coming from the same person with the same communication style.
    """

    # Call OpenAI API   
    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are the dedicated Email Specialist for Fast Book Ads (FBA‑Agent) and you reply from either fastbookads@gmail.com or info@fastbookads.com."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=500,
        temperature=0.7
    )

    ai_response = response.choices[0].message.content.strip()
    print(ai_response)

# gmail_service = get_gmail_service()
# if gmail_service:
#     find_first_conversation_with_reply(gmail_service,pc,index)
